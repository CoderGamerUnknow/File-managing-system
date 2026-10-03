"""Zero-dependency local web dashboard for fs-organizer.

HTTP/session layer only: serves the single-page UI (dashboard.html) and the
JSON API. The view models it serializes live in views.py; the page asset is
dashboard.html next to this file.

  GET /            -> the UI (inline HTML)
  GET /api/status  -> live organizer status + rule/ignore/extension summary
  GET /api/events  -> recent activity (organized / skipped / errors)
  POST /api/once   -> run a one-shot organize scan in the background

The server binds to 127.0.0.1 only (never 0.0.0.0) — it is a local control
panel, not a network service. POST endpoints are protected by a per-process
random token that must be supplied as X-Auth-Token (the UI gets it injected).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Re-exported view models: the dashboard's data layer, kept importable from
# here for backward compatibility with existing callers/tests.
from .views import (  # noqa: F401
    _build_groups,
    _build_months,
    _build_summary,
    _file_category,
    _fmt_size,
    files_payload,
    plan_summary,
    rules_payload,
    status_payload,
)

logger = logging.getLogger("fs_organizer")

_MAX_EVENTS = 500  # ring-buffer cap; older activity is dropped


def _parse_since(raw_path: str) -> int | None:
    """Extract ``?since=N`` (clamped to >= 0); None when absent or malformed."""
    try:
        query = urlsplit(raw_path).query
    except ValueError:
        return None
    values = parse_qs(query).get("since", [])
    if not values:
        return None
    try:
        return max(0, int(values[0]))
    except (TypeError, ValueError):
        return None


@dataclass
class ActivityLog:
    """Thread-safe ring buffer of organizer activity for the UI."""

    max_events: int = _MAX_EVENTS
    events: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Monotonic, gap-free event sequence. Ring-buffer trimming drops old
        # *entries* but never resets the counter, so a client that tracks the
        # highest seq it has seen can ask "events since N" and never see a
        # duplicate or miss one after a wrap (the proven 700→500 bug class).
        self._next_seq = 0
        # V2 runtime stats: cheap integer counters incremented alongside the
        # event ring (no polling, no timers — low-resource-daemon skill).
        # ``paused`` is mirrored here so /api/status reports one truth.
        self.stats = {
            "moved": 0,
            "skipped": 0,
            "refused": 0,
            "dryrun": 0,
            "errors": 0,
            "bytes_moved": 0,
            "ai_calls": 0,
            "paused": False,
        }
        self._stats_started_at: float = time.time()

    @staticmethod
    def _stat_kind(kind: str) -> str | None:
        """Map an event kind to its stats bucket (None = uncounted)."""
        if kind in ("moved", "skipped", "refused", "dryrun", "error"):
            return "errors" if kind == "error" else kind
        return None

    def record_bytes(self, size: int) -> None:
        """Add ``size`` bytes to the bytes_moved counter (called by the
        Organizer when a real move succeeded)."""
        with self.lock:
            self.stats["bytes_moved"] += int(size)

    def record_ai_call(self) -> None:
        with self.lock:
            self.stats["ai_calls"] += 1

    def set_paused(self, paused: bool) -> None:
        with self.lock:
            self.stats["paused"] = bool(paused)

    def stats_snapshot(self) -> dict:
        """Counters since start + uptime seconds (single locked read)."""
        with self.lock:
            out = dict(self.stats)
            out["uptime_seconds"] = int(time.time() - self._stats_started_at)
            return out

    def add(self, kind: str, path, detail: str = "", *, display_name: str | None = None) -> None:
        """Record one activity event.

        `display_name` overrides the name shown in the UI — the watcher passes
        the final (possibly collision-suffixed) file name there, since the
        source path name can differ from what landed in the target root.
        """
        if display_name is not None:
            name = str(display_name)
        else:
            try:
                name = Path(path).name
            except (TypeError, ValueError):
                name = str(path)
        with self.lock:
            seq = self._next_seq
            self._next_seq += 1
            entry = {
                "seq": seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,
                "name": name,
                "detail": detail,
            }
            self.events.append(entry)
            if len(self.events) > self.max_events:
                del self.events[: len(self.events) - self.max_events]
            bucket = self._stat_kind(kind)
            if bucket:
                self.stats[bucket] += 1

    def since(self, seq: int) -> list[dict]:
        """Events with ``seq > N``, oldest first (incremental polling).

        A ``since`` older than the oldest retained event returns the whole
        buffer — the client rebuilds from it; the ring cap means genuinely
        ancient events are gone no matter what seq was asked for.
        """
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(reversed(self.events))  # newest first

    def range(self) -> tuple[int | None, int | None]:
        """(seq of the oldest retained event, seq of the newest).

        Lets a client detect that it has fallen off the ring (polled too
        late) or that the server counter restarted below its watermark —
        both mean "re-fetch the full snapshot".
        """
        with self.lock:
            if not self.events:
                return None, None
            return self.events[0]["seq"], self.events[-1]["seq"]


# Keys allowed in /api/once bodies; anything else is rejected.
_ONCE_ACTIONS = {"scan"}


@dataclass
class DashboardState:
    """Shared state handed to the HTTP handler."""

    def __init__(self, config, activity: ActivityLog, watcher=None, config_path=None) -> None:
        self.config = config
        self.activity = activity
        # V2: the running watcher (pause/resume) and the config file path
        # (live reload). Both optional — a bare Dashboard still works.
        self.watcher = watcher
        self.config_path = Path(config_path) if config_path else None
        self.on_reload = None  # set by the CLI: callable(new_config) -> None
        self.token = secrets.token_hex(16)
        self.oneshot_running = threading.Event()
        self.oneshot_result = {"status": "idle", "moved": 0, "error": None}
        self._oneshot_lock = threading.Lock()

    def run_once_async(self, action: str) -> tuple[bool, str]:
        """Kick off a one-shot scan on a worker thread; returns (started, msg)."""
        if action not in _ONCE_ACTIONS:
            return False, f"Unknown action: {action!r}"
        with self._oneshot_lock:
            if self.oneshot_running.is_set():
                return False, "A scan is already running"
            self.oneshot_running.set()
            self.oneshot_result = {"status": "running", "moved": 0, "error": None}
        threading.Thread(target=self._run_once, daemon=True, name="fs-organizer-oneshot").start()
        return True, "Scan started"

    def _run_once(self) -> None:
        from .__main__ import _one_shot  # local import: avoids CLI import cycle at module load
        try:
            # The exact count is the scan's return value — counting activity
            # events instead would cap at the ring-buffer size and credit the
            # scan with moves the watcher made while it ran.
            moved = _one_shot(self.config, activity=self.activity)
            with self._oneshot_lock:
                self.oneshot_result = {"status": "done", "moved": moved, "error": None}
            self.activity.add("info", "(scan)", "Manual scan finished")
        except Exception as exc:
            logger.exception("Manual one-shot scan failed")
            with self._oneshot_lock:
                self.oneshot_result = {"status": "error", "moved": 0, "error": str(exc)}
            self.activity.add("error", "(scan)", f"Manual scan failed: {exc}")
        finally:
            self.oneshot_running.clear()

    # -- V2 runtime controls -------------------------------------------------

    def pause(self, paused: bool) -> tuple[bool, str]:
        """Pause or resume the watcher. Returns (ok, message)."""
        if self.watcher is None:
            return False, "No watcher is attached (one-shot mode?)"
        if paused:
            self.watcher.pause()
            return True, "Paused — events are being held"
        self.watcher.resume()
        return True, "Resumed — held events dispatched"

    def reload_config(self) -> tuple[bool, str, dict]:
        """Re-read the config file and hot-swap it into the running daemon.

        On any validation error the current config stays active. Returns
        (ok, message, new_status_payload_or_empty).
        """
        if self.config_path is None:
            return False, "No config file path is known (test/embedded setup)", {}
        from .config import ConfigError, load_config

        try:
            new_config = load_config(self.config_path)
        except ConfigError as exc:
            self.activity.add("error", "(reload)", f"Config rejected: {exc}")
            return False, f"Config rejected — previous config stays active: {exc}", {}
        old = self.config
        self.config = new_config
        applied = True
        detail = "Config reloaded"
        # Swap the watcher's config + watch set. The observer rebuilds its
        # watches; debounced/pending work keeps the old config object (its
        # decisions were made under it — safe: guards only get stricter).
        if self.watcher is not None:
            try:
                self.watcher.apply_config(new_config)
            except Exception as exc:
                logger.exception("apply_config failed; reverting")
                self.config = old
                applied = False
                detail = f"Reload failed while re-watching: {exc} — previous config stays active"
                self.activity.add("error", "(reload)", detail)
        if self.on_reload is not None and applied:
            try:
                self.on_reload(new_config)
            except Exception:
                logger.exception("on_reload callback failed")
        if applied:
            self.activity.add("info", "(reload)", detail)
        return applied, detail, status_payload(self.config)


def make_handler(state: DashboardState):
    """Build a request-handler class bound to the shared state."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "fs-organizer-ui/0.1"

        # -- helpers ------------------------------------------------------
        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Local-only control panel: no caching, no cross-site reads.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _client_allowed(self) -> bool:
            # Belt and suspenders: the listener already binds to loopback;
            # this also blocks non-browser clients on shared machines unless
            # (for POSTs) they know the per-process token.
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _authed_post(self) -> bool:
            return self.headers.get("X-Auth-Token", "") == state.token

        def log_message(self, fmt, *args) -> None:  # silence default stderr spam
            logger.debug("ui: " + fmt, *args)

        # -- routes -------------------------------------------------------
        def do_GET(self) -> None:
            if not self._client_allowed():
                self._send_json({"error": "forbidden"}, status=403)
                return
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                body = render_page(state.token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/status":
                payload = status_payload(state.config)
                payload["oneshot"] = dict(state.oneshot_result)
                payload["stats"] = state.activity.stats_snapshot()
                payload["paused"] = state.watcher.paused if state.watcher else False
                # NOTE: the plan summary is deliberately NOT part of
                # /api/status. plan_summary() walks every watch folder
                # (bounded at 2000 files) and the dashboard polls this
                # endpoint every 2s — that turned an idle dashboard into a
                # continuous full-tree scan, against the low-resource-daemon
                # invariant. The plan is available on demand at /api/plan.
                self._send_json(payload)
            elif path == "/api/rules":
                # Read-only config summary: every rule, the effective
                # ignore list, and the coverage gaps. No filesystem access.
                self._send_json(rules_payload(state.config))
            elif path == "/api/events":
                since = _parse_since(self.path)
                if since is None:
                    # Full snapshot (legacy shape): newest first.
                    self._send_json({"events": state.activity.snapshot()})
                else:
                    # Incremental: only seq > since, oldest first, plus the
                    # retained range so the client can detect ring-wrap or a
                    # counter reset and re-sync from a full snapshot.
                    oldest, latest = state.activity.range()
                    self._send_json({
                        "events": state.activity.since(since),
                        "oldest": oldest,
                        "latest": latest,
                    })
            elif path == "/api/files":
                self._send_json(files_payload(state.config))
            elif path == "/api/plan":
                self._send_json(plan_summary(state.config))
            elif path == "/api/dupes":
                # Report-only duplicate scan (bounded: size pre-filter, file
                # cap, hashing budget). GET is safe to expose: it mutates
                # nothing and returns only paths under the watch folders.
                from .duplicates import find_duplicates

                self._send_json(find_duplicates(state.config))
            elif path == "/api/export":
                # V3: download the move journal as CSV or JSON. Read-only
                # GET (loopback-only like every route), so no token needed —
                # a browser <a download> / location.href cannot send one.
                values = parse_qs(urlsplit(self.path).query).get("format", ["csv"])
                fmt = (values[0] if values else "csv").lower()
                if fmt not in ("csv", "json"):
                    self._send_json({"error": "format must be 'csv' or 'json'"}, status=400)
                    return
                from .journal import export_journal

                body = export_journal(state.config, fmt=fmt).encode("utf-8")
                self.send_response(200)
                ctype = "text/csv" if fmt == "csv" else "application/json"
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="moves.{fmt}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            if not self._client_allowed():
                self._send_json({"error": "forbidden"}, status=403)
                return
            path = self.path.split("?", 1)[0]
            if path not in ("/api/once", "/api/pause", "/api/reload", "/api/undo"):
                self._send_json({"error": "not found"}, status=404)
                return
            if not self._authed_post():
                self._send_json({"error": "unauthorized"}, status=401)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                # Cap the body we are willing to read — a hostile local client
                # must not be able to balloon memory via Content-Length.
                if length < 0 or length > 64 * 1024:
                    raise ValueError("oversized body")
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            if not isinstance(data, dict):
                self._send_json({"error": "JSON body must be an object"}, status=400)
                return
            action = data.get("action", "")
            if path == "/api/once":
                ok, msg = state.run_once_async(action)
                self._send_json(
                    {"ok": ok, "message": msg, **state.oneshot_result},
                    status=200 if ok else 409,
                )
                return
            if path == "/api/pause":
                if action not in ("pause", "resume"):
                    self._send_json(
                        {"ok": False, "message": "action must be 'pause' or 'resume'"},
                        status=400,
                    )
                    return
                ok, msg = state.pause(action == "pause")
                self._send_json(
                    {"ok": ok, "message": msg, "paused": state.watcher.paused if state.watcher else False},
                    status=200 if ok else 409,
                )
                return
            if path == "/api/undo":
                # V3: restore recent moves from the journal. Token-protected
                # (it mutates), bounded by an explicit count, and refuses to
                # run when the journal is disabled (there would be nothing to
                # undo — fail loudly instead of silently doing nothing).
                from dataclasses import replace as _replace

                from .undo import undo as _run_undo

                count = data.get("count", 1)
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or not (0 <= count <= 500)
                ):
                    self._send_json(
                        {"ok": False, "message": "count must be an integer between 0 and 500"},
                        status=400,
                    )
                    return
                if not state.config.journal.enabled:
                    self._send_json(
                        {"ok": False,
                         "message": "Journal is disabled — set journal.enabled to record moves"},
                        status=409,
                    )
                    return
                cfg = state.config
                if data.get("dry_run"):
                    cfg = _replace(cfg, dry_run=True)
                results = _run_undo(cfg, count=count)
                self._send_json({
                    "ok": True,
                    "restored": sum(r.moved for r in results),
                    "would_restore": sum(r.would_move for r in results),
                    "skipped": sum(r.skipped for r in results),
                    "results": [
                        {
                            "src": str(r.src) if r.src else None,
                            "dest": str(r.dest),
                            "restored_to": str(r.restored_to) if r.restored_to else None,
                            "moved": r.moved,
                            "would_move": r.would_move,
                            "skipped": r.skipped,
                            "reason": r.reason,
                        }
                        for r in results
                    ],
                })
                return
            # /api/reload
            ok, msg, payload = state.reload_config()
            self._send_json(
                {"ok": ok, "message": msg, "status": payload},
                status=200 if ok else 409,
            )

    return Handler


class Dashboard:
    """Owns the HTTP server thread; start()/stop() are idempotent-ish."""

    def __init__(self, config, activity: ActivityLog | None = None,
                 port: int = 8765, open_browser: bool = True,
                 watcher=None, config_path=None) -> None:
        self.activity = activity if activity is not None else ActivityLog()
        self.port = port
        self.open_browser = open_browser
        self.state = DashboardState(config, self.activity, watcher=watcher, config_path=config_path)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def config(self):
        """The CURRENT config — always the state's (a live reload swaps the
        state's config; a separate Dashboard copy would go stale and become
        a second source of truth)."""
        return self.state.config

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        if self._server is not None:
            return
        try:
            # ThreadingHTTPServer: one thread per request; daemon_threads so a
            # hung connection can never block shutdown.
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(self.state))
        except OSError as exc:
            # Port busy (or blocked): surface a clean error, leave state
            # consistent so start() can be retried on another port.
            self._server = None
            raise RuntimeError(
                f"Cannot bind dashboard to 127.0.0.1:{self.port}: {exc}"
            ) from exc
        self._server.daemon_threads = True
        # A requested port of 0 lets the OS pick a free port; adopt the real
        # bound port so the log line, URL, and browser all point at it.
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fs-organizer-dashboard",
            daemon=True,
        )
        self._thread.start()
        logger.info("Dashboard running at %s (Ctrl+C in the console does not stop it)", self.url)
        if self.open_browser:
            threading.Thread(target=self._open_browser, daemon=True).start()

    def _open_browser(self) -> None:
        try:
            import webbrowser
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001 - headless systems have no browser
            pass

    def stop(self, timeout: float = 2.0) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None


_HTML_PATH = Path(__file__).with_name("dashboard.html")


def render_page(token: str = "") -> str:
    """Render the dashboard HTML with the POST token injected."""
    return _HTML_PATH.read_text(encoding="utf-8").replace("__TOKEN__", token)
