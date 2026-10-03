"""Append-only move journal (JSONL): the audit trail and the dashboard's
source of truth for creation dates.

One entry per successful move::

    {"ts": 1767000000.123, "src": "C:/.../a.txt", "dest": "C:/.../Documents/a.txt",
     "category": "Documents", "size": 123}

Owner of everything journal: appending, reading (with cursor support),
corrupt-line tolerance. Nobody else touches the file. The journal is
best-effort persistence: an unwritable path degrades to "no journal"
(warning logged) — recording must never break moving.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("fs_organizer")

_journal_lock = threading.Lock()  # serializes appends across worker threads


def _category_from_dest(dest: Path, config) -> str:
    """Derive the category as the first path segment below the target root.

    Mirrors the dashboard's ``_file_category``: the mover places files at
    ``<root>/<Category>[/YYYY-MM]/name``, so the category is always the
    first segment under the root. A file sitting directly in the root (or
    outside it, for a hand-built dest) has no category -> "".
    Pure path math; never touches the filesystem and never raises.
    """
    try:
        root = config.resolved_target_root()
        rel = Path(dest).resolve().relative_to(root)
    except Exception:  # noqa: BLE001 - derivation must never break a move
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


def DEFAULT_JOURNAL_PATH() -> Path:
    """~/.fs-organizer/moves.jsonl (created on first append)."""
    return Path.home() / ".fs-organizer" / "moves.jsonl"


def append_move(config, src: Path, dest: Path, size: int, category: str | None = None) -> None:
    """Record one successful move. Never raises; silently no-ops when disabled.

    Writes are line-buffered and lock-serialized so concurrent worker moves
    produce whole lines even on a crash mid-run.

    ``category`` is the authoritative value when given (the mover knows the
    rule/AI category that decided the move). When omitted, it is derived
    from the destination path as the first segment under the *target root* —
    NOT ``dest.parent.name``, which with ``use_date_subfolders`` is the
    ``YYYY-MM`` month folder and would mislabel the entry (flaw #40).
    """
    if not getattr(config, "journal", None) or not config.journal.enabled:
        return
    if category is None:
        category = _category_from_dest(dest, config)
    entry = {
        "ts": time.time(),
        "src": str(src),
        "dest": str(dest),
        "category": category,
        "size": int(size),
    }
    try:
        path = config.journal.resolved_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _journal_lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        logger.warning("Could not write move journal: %s", exc)


def read_entries(path: Path, since_ts: float | None = None) -> list[dict]:
    """Read journal entries newer than ``since_ts`` (None = all).

    Tolerates a torn final line (crash mid-append): partial JSON lines are
    skipped, not fatal. Never raises for missing files.
    """
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # torn line from a crash; skip it
                if not isinstance(entry, dict) or "ts" not in entry:
                    continue
                if since_ts is not None and entry["ts"] <= since_ts:
                    continue
                entries.append(entry)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read move journal %s: %s", path, exc)
    return entries


def export_journal(config, fmt: str = "csv") -> str:
    """Serialize the full move journal as CSV or JSON text.

    Read-only: never writes, never raises for a missing/disabled journal
    (returns an empty document instead). Timestamps are emitted both as the
    raw epoch and as ISO-8601 local time so spreadsheets stay readable.
    """
    if not getattr(config, "journal", None) or not config.journal.enabled:
        entries: list[dict] = []
    else:
        entries = read_entries(config.journal.resolved_path())

    rows = []
    for e in entries:
        ts = e.get("ts", 0)
        try:
            iso = datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
        except (OverflowError, OSError, TypeError, ValueError):
            iso = ""
        rows.append(
            {
                "ts": ts,
                "ts_iso": iso,
                "src": e.get("src", ""),
                "dest": e.get("dest", ""),
                "category": e.get("category", ""),
                "size": e.get("size", ""),
            }
        )

    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"

    # csv module handles quoting/escaping correctly for all fields.
    buf = io.StringIO()
    fields = ["ts", "ts_iso", "src", "dest", "category", "size"]
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
