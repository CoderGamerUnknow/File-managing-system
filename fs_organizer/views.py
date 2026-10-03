"""Dashboard view models: read-only payload builders over a resolved Config.

Pure data — no HTTP, no sockets. The dashboard (ui.py) serializes these
dicts; the CLI or tests can consume the same builders directly.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from . import journal
from .mover import plan


def _walk_bounded(root: Path, max_files: int) -> tuple[list[Path], bool]:
    """Iterative walk of *root* collecting up to max_files regular files.

    Deliberately not recursive-by-generator: an explicit stack with a hard
    file cap keeps the dashboard responsive even if the user points the
    target root at a huge tree.

    Returns ``(found, truncated)``. ``truncated`` is True only when a file
    was seen *after* the cap was reached — a tree with exactly ``max_files``
    files is not truncation.
    """
    found: list[Path] = []
    truncated = False
    stack: list[Path] = [root]
    while stack and not truncated:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if len(found) >= max_files:
                truncated = True
                break
            try:
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    found.append(entry)
            except OSError:
                continue
    return found, truncated


def _file_category(path: Path, root: Path) -> str:
    """The category shown for *path* in the dashboard.

    The mover places files at ``root/<category>[/YYYY-MM]/...``, so the
    category is always the first path segment below the target root; a file
    sitting directly in the target root has no category. A path outside the
    root (e.g. a symlink target that escapes it) also has no category —
    never raise, the dashboard must survive any tree it is pointed at.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ""
    return relative.parts[0] if len(relative.parts) > 1 else ""


def _build_groups(rows: list[dict]) -> dict[str, list[dict]]:
    """Group file rows by their creation date, newest day first.

    Pure helper (no filesystem access) so grouping is testable portably.
    Each row: {name, category, created, size}; `created` is a POSIX timestamp.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        date_key = datetime.fromtimestamp(row["created"]).strftime("%Y-%m-%d")
        groups.setdefault(date_key, []).append(row)
    return {day: groups[day] for day in sorted(groups, reverse=True)}


def _build_summary(groups: dict[str, list[dict]]) -> dict[str, dict]:
    """Per-day rollup: {"YYYY-MM-DD": {"count": n, "bytes": n, "categories": {cat: n}}}."""
    summary: dict[str, dict] = {}
    for day, files in groups.items():
        cats: dict[str, int] = {}
        total_bytes = 0
        for f in files:
            cat = f["category"] or "(root)"
            cats[cat] = cats.get(cat, 0) + 1
            total_bytes += f["size"]
        summary[day] = {
            "count": len(files),
            "bytes": total_bytes,
            "categories": dict(sorted(cats.items())),
        }
    return summary


def _build_months(groups: dict[str, list[dict]]) -> list[dict]:
    """Month index for the UI's month/year navigation, newest first.

    Each entry: {"key": "YYYY-MM", "year": 2026, "month": 9, "label": "September 2026",
    "count": n, "days": ["YYYY-MM-DD", ...]}.
    """
    months: dict[str, dict] = {}
    for day, files in groups.items():
        key = day[:7]
        entry = months.setdefault(
            key,
            {"key": key, "year": int(key[:4]), "month": int(key[5:7]), "count": 0, "days": []},
        )
        entry["count"] += len(files)
        entry["days"].append(day)
    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    out = []
    for key in sorted(months, reverse=True):
        entry = months[key]
        entry["label"] = f"{month_names[entry['month']]} {entry['year']}"
        entry["days"].sort(reverse=True)
        out.append(entry)
    return out


def _journal_dates(config) -> dict[str, float]:
    """Map destination path (normalized) -> journal timestamp.

    The journal records the moment fs-organizer moved each file. On a
    filesystem without birthtime, a moved file's mtime equals the move time,
    which would mislabel its creation date forever. The journal's ``ts`` is
    the authoritative "arrived in the organized tree" date.

    The read is bounded and best-effort: any failure degrades to an empty map
    (the dashboard then falls back to stat-based dates).
    """
    try:
        path = config.journal.resolved_path()
    except Exception:  # noqa: BLE001 - a broken journal must never break /api/files
        return {}
    try:
        entries = journal.read_entries(path)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for entry in entries:
        dest = entry.get("dest")
        ts = entry.get("ts")
        if isinstance(dest, str) and isinstance(ts, (int, float)):
            out[os.path.normcase(dest)] = float(ts)
    return out


def files_payload(config, max_files: int = 2000) -> dict:
    """List files under the target root grouped by their creation date.

    Each entry carries a POSIX `created` timestamp. On Windows this is the
    file's birth time; on filesystems that don't track creation time (ext4
    without birthtime, older macOS APIs) it falls back to the last metadata
    change or modification time — so the date is always meaningful.

    When the move journal is enabled it is the preferred source for files it
    has an entry for: the journal's ``ts`` says when the file was organized,
    which is exactly what a filesystem without birthtime cannot tell. Files
    predating the journal (or with journaling off) still use the stat
    fallback, so behavior without the journal is unchanged.
    """
    root = config.resolved_target_root()
    journal_dates = _journal_dates(config)
    rows: list[dict] = []
    try:
        entries, truncated = _walk_bounded(root, max_files)
    except OSError:
        entries, truncated = [], False
    for path in entries:
        try:
            stat = path.stat()
        except OSError:
            continue
        created = journal_dates.get(os.path.normcase(str(path)))
        if created is None:
            created = getattr(stat, "st_birthtime", None) or stat.st_mtime
        rows.append({
            "name": path.name,
            "category": _file_category(path, root),
            "created": int(created),
            "size": stat.st_size,
        })
    groups = _build_groups(rows)
    return {
        "target_root": str(root),
        "total": len(rows),
        "truncated": truncated,
        "groups": groups,
        "summary": _build_summary(groups),
        "months": _build_months(groups),
    }


def status_payload(config) -> dict:
    """Summarize the running config for the dashboard."""
    rules = dict(sorted(config.target_rules.items()))
    # Invert to category -> [ext, ...] for a friendlier UI table.
    by_category: dict[str, list[str]] = {}
    for ext, cat in rules.items():
        by_category.setdefault(cat, []).append(ext)

    ai = config.ai
    return {
        "watch_folders": [str(p) for p in config.resolved_watch_folders()],
        "target_root": str(config.resolved_target_root()),
        "categories": {
            cat: sorted(exts) for cat, exts in sorted(by_category.items())
        },
        "ignore_patterns": list(config.effective_ignore_patterns()),
        "dry_run": config.dry_run,
        "use_date_subfolders": config.use_date_subfolders,
        "recursive": config.recursive,
        "file_stable_seconds": config.file_stable_seconds,
        "age_policy": config.age_policy.to_dict(),
        "destination_template": config.destination_template.to_dict(),
        "sub_rules": [r.to_dict() for r in config.sub_rules],
        "ai": {
            "enabled": ai.enabled,
            "provider": ai.provider,
            "model": ai.model,
            "extensions": list(ai.extensions),
            "allowed_subfolders": list(ai.allowed_subfolders),
        },
    }


def _fmt_size(bytes_value: float) -> str:
    """Human readable size for the dashboard status card."""
    if not bytes_value >= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    value, unit_index = bytes_value, 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.1f} {units[unit_index]}"


def plan_summary(config, max_files: int = 2000) -> dict:
    """Plan preview for the dashboard status card.

    Counts how many files would be organised and how large they are, without
    mutating the filesystem. ``max_files`` bounds the count so a deep watch
    folder can never blow the dashboard's memory.
    """
    data = plan(config, max_files=max_files)
    # ``plan()`` only reports files it could stat; a file that vanished
    # between the scan and the read is reported once and dropped (see
    # ``plan()``), so every row is guaranteed to have a ``size``.
    total_bytes = sum(r["size"] for r in data["rows"])
    return {
        "target_root": data["target_root"],
        "watch_folders": data["watch_folders"],
        "would_move": data["would_move"],
        "file_count": data["total"],
        "bytes": total_bytes,
        "bytes_human": _fmt_size(total_bytes),
        "truncated": data["truncated"],
    }


def rules_payload(config) -> dict:
    """Rule + AI coverage report for the dashboard's Rules card.

    Shows the effective category table *and* the gaps the user should care
    about: extensions that are configured but unmatchable, AI-able
    extensions that are not in the AI allow-list, and any AI-allowed
    category the rules never mention. It is derived purely from the config,
    so it is deterministic and costs nothing to compute.
    """
    from .mover import plan_actions

    cover = config.coverage_report()
    # The folders the organizer actually acts on (existing, resolved) — not
    # expanded_watch_folders(), which can list configured-but-missing folders
    # and would advertise work the watcher will never do (flaw #42).
    resolved = config.resolved_watch_folders()
    actions = plan_actions(config)

    return {
        "watch_folders": [str(p) for p in resolved],
        "expanded_watch_folders": [str(p) for p in config.expanded_watch_folders()],
        "categories": cover["categories"],
        "ignored": config.effective_ignore_patterns(),
        "dry_run": config.dry_run,
        "use_date_subfolders": config.use_date_subfolders,
        "file_stable_seconds": config.file_stable_seconds,
        "coverage": {
            "known_extensions": cover["known_extensions"],
            "uncovered": cover["uncovered"],
            "ai_covered": cover["ai_covered"],
            "rule_categories": cover["rule_categories"],
            "ai_categories": cover["ai_categories"],
            "mismatched_categories": cover["mismatched_categories"],
        },
        "sub_rules": [r.to_dict() for r in config.sub_rules],
        "action_counts": actions["counts"],
    }
