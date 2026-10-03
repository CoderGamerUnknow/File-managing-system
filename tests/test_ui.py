"""Tests for the local web dashboard (fs_organizer.ui)."""
import json
import time
import urllib.request

import pytest

from fs_organizer import journal
from fs_organizer.config import JournalConfig
from fs_organizer.ui import (
    ActivityLog,
    Dashboard,
    status_payload,
)
from helpers import make_config as _make_config


def make_config(tmp_path):
    """File-local default: this suite's fixtures also map .png -> Images."""
    return _make_config(tmp_path, rules={".txt": "Documents", ".png": "Images"})


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestActivityLog:
    def test_add_and_snapshot_newest_first(self):
        log = ActivityLog()
        log.add("moved", "a.txt", "-> out")
        log.add("skipped", "b.tmp")
        events = log.snapshot()
        assert [e["kind"] for e in events] == ["skipped", "moved"]
        assert events[0]["name"] == "b.tmp"

    def test_ring_buffer_cap(self):
        log = ActivityLog(max_events=5)
        for i in range(20):
            log.add("moved", f"f{i}.txt")
        events = log.snapshot()
        assert len(events) == 5
        # Newest kept, oldest dropped.
        assert events[0]["name"] == "f19.txt"

    def test_survives_unusual_paths(self):
        log = ActivityLog()
        log.add("moved", 12345)  # non-path junk must not raise
        assert log.snapshot()[0]["name"] == "12345"


class TestStatusPayload:
    def test_groups_rules_by_category(self, tmp_path):
        cfg = make_config(tmp_path)
        s = status_payload(cfg)
        assert s["categories"] == {
            "Documents": [".txt"],
            "Images": [".png"],
        }
        assert s["watch_folders"] == [str(tmp_path / "watch")]
        assert s["target_root"] == str(tmp_path / "out")
        assert s["dry_run"] is False

    def test_ai_summary(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.provider = "ollama"
        cfg.ai.extensions = [".xyz"]
        s = status_payload(cfg)
        assert s["ai"]["enabled"] is True
        assert s["ai"]["extensions"] == [".xyz"]


def _wait_for_server(url: str, timeout: float = 5.0) -> None:
    """Poll until the test server accepts connections (CI machines are slow)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is up (it answered with an HTTP status)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.05)
    raise AssertionError(f"server never came up at {url}: {last_exc}")


class TestDashboard:
    def test_serves_page_and_api(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            html = urllib.request.urlopen(dash.url, timeout=5).read().decode("utf-8")
            assert "fs-organizer" in html
            assert dash.state.token in html  # token injected into the page

            status = json.loads(urllib.request.urlopen(dash.url + "api/status", timeout=5).read())
            assert status["target_root"] == str(tmp_path / "out")
            assert status["oneshot"]["status"] == "idle"

            events = json.loads(urllib.request.urlopen(dash.url + "api/events", timeout=5).read())
            assert events == {"events": []}
        finally:
            dash.stop()

    def test_post_requires_token(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=json.dumps({"action": "scan"}).encode(),
                headers={"Content-Type": "application/json"},  # no token
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 401
        finally:
            dash.stop()

    def test_post_with_token_runs_scan(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=json.dumps({"action": "scan"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Auth-Token": dash.state.token,
                },
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            assert resp["ok"] is True

            # The scan runs async; wait for it to finish.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                st = json.loads(urllib.request.urlopen(dash.url + "api/status", timeout=5).read())
                if st["oneshot"]["status"] == "done":
                    break
                time.sleep(0.05)
            assert st["oneshot"]["status"] == "done"
            assert st["oneshot"]["moved"] == 1
            assert not src.exists()
            assert (tmp_path / "out" / "Documents" / "a.txt").exists()
            events = json.loads(urllib.request.urlopen(dash.url + "api/events", timeout=5).read())
            kinds = [e["kind"] for e in events["events"]]
            assert "moved" in kinds
        finally:
            dash.stop()

    def test_no_token_no_404_leaks(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(dash.url + "api/nope", timeout=5)
            assert exc_info.value.code == 404
        finally:
            dash.stop()

    def test_stop_is_idempotent(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        dash.stop()
        dash.stop()  # must not raise
        assert dash._server is None

    def test_starts_only_once(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        try:
            first = dash._server
            dash.start()  # second start must be a no-op, not a port clash
            assert dash._server is first
        finally:
            dash.stop()


class TestFilesPayload:
    def test_build_groups_sorts_days_newest_first(self):
        from fs_organizer.ui import _build_groups

        # Pure-function test: portable to every OS/FS (birthtime isn't settable,
        # so creation dates can't be faked directly on real files).
        day1 = 1_790_000_000  # 2026-09-15-ish (local time)
        day2 = day1 + 86_400  # one day later
        rows = [
            {"name": "older.txt", "category": "Docs", "created": day1, "size": 1},
            {"name": "newer.txt", "category": "Docs", "created": day2, "size": 2},
            {"name": "other.txt", "category": "Pics", "created": day1 + 60, "size": 3},
        ]
        groups = _build_groups(rows)
        days = list(groups)
        assert days == sorted(days, reverse=True), "groups must be newest day first"
        assert [f["name"] for f in groups[days[0]]] == ["newer.txt"]
        assert {f["name"] for f in groups[days[1]]} == {"older.txt", "other.txt"}

    def test_summary_per_day_counts_bytes_categories(self):
        from fs_organizer.ui import _build_groups, _build_summary

        day = 1_790_000_000
        rows = [
            {"name": "a.txt", "category": "Documents", "created": day, "size": 100},
            {"name": "b.txt", "category": "Documents", "created": day + 1, "size": 50},
            {"name": "c.png", "category": "Images", "created": day + 2, "size": 25},
            {"name": "loose", "category": "", "created": day + 3, "size": 5},
        ]
        summary = _build_summary(_build_groups(rows))
        (entry,) = summary.values()
        assert entry["count"] == 4
        assert entry["bytes"] == 180
        assert entry["categories"] == {"(root)": 1, "Documents": 2, "Images": 1}

    def test_months_index_newest_first(self):
        from fs_organizer.ui import _build_groups, _build_months

        sep1 = 1_790_000_000  # 2026-09-21 local
        sep2 = sep1 + 86_400  # next day, same month
        aug1 = 1_787_400_000  # 2026-08-22 local
        rows = [
            {"name": "a", "category": "D", "created": sep1, "size": 1},
            {"name": "b", "category": "D", "created": sep2, "size": 1},
            {"name": "c", "category": "D", "created": aug1, "size": 1},
        ]
        months = _build_months(_build_groups(rows))
        assert [m["key"] for m in months] == sorted(
            [m["key"] for m in months], reverse=True
        )
        assert months[0]["count"] == 2 and months[1]["count"] == 1
        assert months[0]["label"].endswith("2026")
        assert len(months[0]["days"]) == 2

    def test_payload_includes_summary_and_months(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out" / "Documents"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")

        data = files_payload(make_config(tmp_path))
        assert set(data["summary"]) == set(data["groups"])
        day = next(iter(data["summary"]))
        assert data["summary"][day]["count"] == 1
        assert data["summary"][day]["bytes"] == 1
        assert len(data["months"]) == 1
        assert data["months"][0]["count"] == 1
        assert data["months"][0]["days"] == [day]

    def test_files_scanned_and_counted(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out"
        docs = root / "Documents"
        docs.mkdir(parents=True)
        (docs / "a.txt").write_text("1234", encoding="utf-8")
        (root / "b.png").write_bytes(b"xx")

        data = files_payload(make_config(tmp_path))
        assert data["total"] == 2
        assert data["truncated"] is False
        assert data["target_root"] == str(root)
        all_rows = [f for files in data["groups"].values() for f in files]
        by_name = {f["name"]: f for f in all_rows}
        assert by_name["a.txt"]["category"] == "Documents"
        assert by_name["a.txt"]["size"] == 4
        assert by_name["b.png"]["category"] == ""  # directly in the root
        assert isinstance(by_name["b.png"]["created"], int)

    def test_empty_root_returns_empty_groups(self, tmp_path):
        from fs_organizer.ui import files_payload

        data = files_payload(make_config(tmp_path))
        assert data["groups"] == {} and data["total"] == 0
        assert data["target_root"] == str(tmp_path / "out")

    def test_missing_root_returns_empty_groups(self, tmp_path):
        from fs_organizer.ui import files_payload

        cfg = make_config(tmp_path)
        cfg.target_root = str(tmp_path / "never-created")
        data = files_payload(cfg)
        assert data["groups"] == {} and data["total"] == 0

    def test_max_files_cap_sets_truncated(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out"
        root.mkdir(parents=True)
        for i in range(5):
            (root / f"f{i}.txt").write_text("x", encoding="utf-8")
        data = files_payload(make_config(tmp_path), max_files=3)
        assert data["total"] == 3
        assert data["truncated"] is True

    def test_files_endpoint_served(self, tmp_path):
        cfg = make_config(tmp_path)
        root = tmp_path / "out" / "Documents"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")

        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            data = json.loads(
                urllib.request.urlopen(dash.url + "api/files", timeout=5).read()
            )
            assert data["total"] == 1
            day = next(iter(data["groups"]))
            assert data["groups"][day][0]["name"] == "a.txt"
            # The served page must reference the new card and endpoint.
            html = urllib.request.urlopen(dash.url, timeout=5).read().decode("utf-8")
            assert "Files by date" in html
            assert "/api/files" in html
            # Month navigation + size formatting are wired into the page.
            assert "month-select" in html
            assert "month-prev" in html and "month-next" in html
            assert "fmtSize" in html
        finally:
            dash.stop()


class TestActivityLogDisplayName:
    def test_display_name_overrides_path_name(self):
        log = ActivityLog()
        log.add("moved", "C:/watch/a.txt", "-> out", display_name="a (1).txt")
        assert log.snapshot()[0]["name"] == "a (1).txt"

    def test_display_name_survives_number_paths(self):
        log = ActivityLog()
        log.add("moved", 12345, "", display_name="x.bin")
        assert log.snapshot()[0]["name"] == "x.bin"


class TestDashboardErrorPaths:
    def test_start_failure_raises_and_state_consistent(self, tmp_path, monkeypatch):
        """A bind failure becomes a clean RuntimeError; state stays retryable.

        (Port-in-use can't be simulated portably — Windows SO_REUSEADDR lets a
        second listener bind the same port — so patch the constructor.)
        """
        import fs_organizer.ui as ui_mod

        def boom(*a, **k):
            raise OSError(48, "Address already in use")

        monkeypatch.setattr(ui_mod, "ThreadingHTTPServer", boom)
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        with pytest.raises(RuntimeError, match="Cannot bind dashboard"):
            dash.start()
        assert dash._server is None  # retryable, not stuck started

    def test_stop_after_failed_start_is_safe(self, tmp_path):
        import socket

        cfg = make_config(tmp_path)
        # Occupy a port with a plain socket so the dashboard cannot bind it.
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        dash = Dashboard(cfg, port=port, open_browser=False)
        try:
            with pytest.raises(RuntimeError):
                dash.start()
            dash.stop()  # must not raise even though start failed
            assert dash._server is None
        finally:
            blocker.close()

    def test_oversized_post_body_rejected(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=b"x" * (65 * 1024 + 1),
                headers={"Content-Type": "application/json",
                         "X-Auth-Token": dash.state.token},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            except OSError:
                # The handler answers 400 and closes while the client is still
                # uploading; Windows then aborts the client socket (WinError
                # 10053) before the response is delivered. A mid-upload abort
                # IS the rejection — treat it as a pass.
                pass
            else:
                pytest.fail("server accepted an oversized body")
        finally:
            dash.stop()


class TestWatcherActivityFeed:
    def test_organizer_reports_to_activity(self, tmp_path):
        from fs_organizer.ui import ActivityLog
        from fs_organizer.watcher import Organizer

        cfg = make_config(tmp_path)
        activity = ActivityLog()
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        Organizer(cfg, activity=activity).handle(src)
        kinds = [e["kind"] for e in activity.snapshot()]
        assert "moved" in kinds

    def test_watcher_accepts_activity_kwarg(self, tmp_path):
        from fs_organizer.watcher import Watcher

        cfg = make_config(tmp_path)
        activity = ActivityLog()
        watcher = Watcher(cfg, activity=activity)
        assert watcher.organizer.activity is activity
        watcher.start()
        watcher.stop()  # full start/stop cycle must work with the feed attached


class TestDashboardEscaping:
    """Invariant 7: the dashboard must escape user-influenced strings
    before rendering. renderStatus() interpolated ${v} raw, so a config
    value like an <img onerror=...> target root executed script (flaw #49)."""

    def _page(self, tmp_path, **overrides):
        cfg = make_config(tmp_path)
        for key, value in overrides.items():
            setattr(cfg, key, value)
        dash = Dashboard(cfg, port=0, open_browser=False)
        dash.start()
        try:
            with urllib.request.urlopen(dash.url, timeout=10) as resp:
                return resp.read().decode("utf-8")
        finally:
            dash.stop()

    def test_status_rows_escape_config_values(self, tmp_path):
        page = self._page(tmp_path, target_root="<img src=x onerror=alert(1)>")
        # Every config-derived value rendered into #status must pass through
        # escapeHtml() at the point of interpolation.
        for needle in (
            "escapeHtml(s.target_root)",
            "escapeHtml(s.file_stable_seconds",
            "escapeHtml(s.use_date_subfolders",
            "escapeHtml(s.recursive",
            "escapeHtml(ai)",
            "escapeHtml(s.ignore_patterns.length",
        ):
            assert needle in page, f"status row value is not escaped: {needle}"
        # ${v} is still the render loop's placeholder, but its ONLY source is
        # the `rows` array built above — every element of which is escaped (or
        # pre-escaped markup for ai/subRules). A row entry built from a bare
        # config value would reintroduce the injection, so assert the rows
        # array is the sole supplier.
        assert page.count("${v}") == 1, (
            "unexpected number of ${v} interpolations in the dashboard"
        )
        assert "const rows = [" in page, (
            "status rows must be assembled (escaped) before interpolation"
        )

    def test_sub_rule_pattern_and_category_are_escaped(self, tmp_path):
        from fs_organizer.config import SubRule

        cfg = make_config(tmp_path)
        cfg.sub_rules = [
            SubRule(pattern="<b>x</b>", extensions=["<i>.pdf</i>"], category="<s>c</s>")
        ]
        dash = Dashboard(cfg, port=0, open_browser=False)
        dash.start()
        try:
            with urllib.request.urlopen(dash.url, timeout=10) as resp:
                page = resp.read().decode("utf-8")
        finally:
            dash.stop()
        assert "escapeHtml(r.pattern)" in page
        assert "escapeHtml(r.extensions.join" in page
        assert "escapeHtml(r.category)" in page

    def test_file_and_event_names_stay_escaped(self, tmp_path):
        """The pre-existing escaping for names must not regress."""
        page = self._page(tmp_path)
        for needle in (
            "escapeHtml(f.name)",
            "escapeHtml(f.category",
            "escapeHtml(e.name)",
            "escapeHtml(e.detail)",
            "escapeHtml(g.hash",
            "escapeHtml(f.path)",
        ):
            assert needle in page, f"lost escaping for {needle}"


class TestUndoAndExportEndpoints:
    """V3 dashboard endpoints: journal export download + token-gated undo."""

    def _dash(self, tmp_path, journal_enabled=True):
        cfg = make_config(tmp_path)
        if journal_enabled:
            cfg.journal = JournalConfig(
                enabled=True, path=str(tmp_path / "moves.jsonl")
            )
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        return cfg, dash

    @staticmethod
    def _post(dash, path, body, with_token=True):
        headers = {"Content-Type": "application/json"}
        if with_token:
            headers["X-Auth-Token"] = dash.state.token
        req = urllib.request.Request(
            dash.url + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _seed_move(self, cfg, tmp_path):
        """Simulate one organized move: dest file exists + journal entry."""
        src = tmp_path / "watch" / "a.txt"
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("payload", encoding="utf-8")
        journal.append_move(cfg, src, dest, 7, "Documents")
        return src, dest

    # -- export ----------------------------------------------------------

    def test_export_csv_download(self, tmp_path):
        cfg, dash = self._dash(tmp_path)
        self._seed_move(cfg, tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            resp = urllib.request.urlopen(dash.url + "api/export?format=csv", timeout=5)
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/csv")
            assert 'attachment; filename="moves.csv"' in resp.headers["Content-Disposition"]
            lines = resp.read().decode("utf-8").strip().splitlines()
            assert lines[0] == "ts,ts_iso,src,dest,category,size"
            assert len(lines) == 2
            assert str(tmp_path / "watch" / "a.txt") in lines[1]
        finally:
            dash.stop()

    def test_export_json_download(self, tmp_path):
        cfg, dash = self._dash(tmp_path)
        self._seed_move(cfg, tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            resp = urllib.request.urlopen(dash.url + "api/export?format=json", timeout=5)
            rows = json.loads(resp.read())
            assert len(rows) == 1
            assert rows[0]["category"] == "Documents"
            assert "json" in resp.headers["Content-Type"]
        finally:
            dash.stop()

    def test_export_rejects_bad_format(self, tmp_path):
        _cfg, dash = self._dash(tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(dash.url + "api/export?format=xml", timeout=5)
            assert exc_info.value.code == 400
        finally:
            dash.stop()

    def test_export_disabled_journal_is_header_only(self, tmp_path):
        _cfg, dash = self._dash(tmp_path, journal_enabled=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            body = urllib.request.urlopen(
                dash.url + "api/export?format=csv", timeout=5
            ).read().decode("utf-8")
            assert body.strip().splitlines() == ["ts,ts_iso,src,dest,category,size"]
        finally:
            dash.stop()

    # -- undo ------------------------------------------------------------

    def test_undo_requires_token(self, tmp_path):
        _cfg, dash = self._dash(tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            status, data = self._post(
                dash, "api/undo", {"action": "undo", "count": 1}, with_token=False
            )
            assert status == 401
            assert data["error"] == "unauthorized"
        finally:
            dash.stop()

    def test_undo_restores_file_with_token(self, tmp_path):
        cfg, dash = self._dash(tmp_path)
        src, dest = self._seed_move(cfg, tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            status, data = self._post(dash, "api/undo", {"action": "undo", "count": 1})
            assert status == 200
            assert data["ok"] is True
            assert data["restored"] == 1
            assert src.read_text(encoding="utf-8") == "payload"
            assert not dest.exists()
        finally:
            dash.stop()

    def test_undo_dry_run_flag_moves_nothing(self, tmp_path):
        cfg, dash = self._dash(tmp_path)
        src, dest = self._seed_move(cfg, tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            status, data = self._post(
                dash, "api/undo", {"action": "undo", "count": 1, "dry_run": True}
            )
            assert status == 200
            assert data["would_restore"] == 1
            assert dest.exists() and not src.exists()
        finally:
            dash.stop()

    def test_undo_journal_disabled_conflicts(self, tmp_path):
        _cfg, dash = self._dash(tmp_path, journal_enabled=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            status, data = self._post(dash, "api/undo", {"action": "undo"})
            assert status == 409
            assert "Journal is disabled" in data["message"]
        finally:
            dash.stop()

    @pytest.mark.parametrize("count", [9999, -1, "abc", True, 1.5])
    def test_undo_rejects_bad_count(self, tmp_path, count):
        _cfg, dash = self._dash(tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            status, data = self._post(dash, "api/undo", {"action": "undo", "count": count})
            assert status == 400
            assert data["ok"] is False
        finally:
            dash.stop()

    # -- page affordances ------------------------------------------------

    def test_page_has_undo_export_and_filter_controls(self, tmp_path):
        _cfg, dash = self._dash(tmp_path)
        dash.start()
        _wait_for_server(dash.url)
        try:
            page = urllib.request.urlopen(dash.url, timeout=5).read().decode("utf-8")
            assert 'id="undo-btn"' in page
            assert 'id="export-btn"' in page
            assert 'id="file-filter"' in page
            assert "/api/export?format=csv" in page
            # The undo POST must carry the per-process token.
            assert '"X-Auth-Token": TOKEN' in page
        finally:
            dash.stop()
