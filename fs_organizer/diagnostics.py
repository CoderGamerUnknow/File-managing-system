"""Diagnostic reporting: the data behind ``check``/``watch-diag`` and their
human-readable renderings. Pure reporting — no CLI concerns, no process I/O
beyond the bounded scans it documents.
"""

from __future__ import annotations

from pathlib import Path

from .rules import is_ignored


def is_ignored_effective(path: Path, config) -> bool:
    """Does the *effective* ignore list (defaults + user) match this path?"""
    return is_ignored(path, config.effective_ignore_patterns())


def _list_children(folder: Path) -> list[Path]:
    """sorted(folder.iterdir()); a folder that vanishes mid-scan yields []."""
    try:
        return sorted(folder.iterdir())
    except OSError:
        return []


def check_report(config, watch_files: dict[str, bool]) -> str:
    """Human-readable view of the fully resolved config (the ``check`` command)."""
    lines: list[str] = []
    add = lines.append
    expanded = config.expanded_watch_folders()
    resolved = config.resolved_watch_folders()
    # The watched set is what the organizer will actually act on. Listing
    # raw configured paths advertised missing folders as watched — the
    # opposite of this command's job ("print what it actually sees"), so
    # missing ones are called out explicitly (flaw #44, flaw #42's class).
    add(f"  watch_folders: {[str(p) for p in resolved]}")
    missing = [str(p) for p in expanded if not p.is_dir()]
    if missing:
        add(f"    CONFIGURED BUT MISSING (not watched): {missing}")
    add(
        f"  target_root:   "
        f"{config.resolved_target_root() if config.target_root else '~/Organized (default)'}"
    )
    add(f"  dry_run:       {config.dry_run}")
    add(f"  use_date_subfolders: {config.use_date_subfolders}")
    add(f"  file_stable_seconds:   {config.file_stable_seconds}")
    add(f"  effective rules: {len(config.target_rules)} extension map(s)")
    for ext, cat in sorted(config.target_rules.items()):
        add(f"    .{ext[1:]} -> {cat}")
    if config.sub_rules:
        add(f"  sub_rules ({len(config.sub_rules)}):")
        for i, rule in enumerate(config.sub_rules):
            add(
                f"    [{i}] {rule.extensions} in {rule.pattern} -> {rule.category}"
            )
    add(f"  effective ignore patterns ({len(config.effective_ignore_patterns())}):")
    for pat in config.effective_ignore_patterns():
        add(f"    {pat}")
    if config.ai.enabled:
        add(f"  ai: {config.ai.enabled} ({config.ai.provider}, model={config.ai.model})")
        add(f"    extensions={config.ai.extensions}")
        add(f"    allowed_subfolders={config.ai.allowed_subfolders}")
    else:
        add("  ai: off")
    cov = config.coverage_report()
    add(f"  coverage: {len(cov['known_extensions'])} known extension(s)")
    if cov.get("uncovered"):
        add(f"    UNCOVERED / no-rule: {', '.join(cov['uncovered'])}")
    if cov.get("mismatched_categories"):
        add(
            f"    CATEGORY MISMATCH between rules and AI allow-list: "
            f"{', '.join(cov['mismatched_categories'])}"
        )
    for path, ignored in sorted(watch_files.items()):
        add(f"ignore_check: {path}  {'IGNORED' if ignored else 'ok'}")
    return "\n".join(lines)


def watch_diag(config, watch_files: list[str]) -> dict[str, object]:
    """What the effective ignore list decides about each watch folder's files.

    Six questions a user actually asks before blaming a rule:
    1. Is the watch folder even watched?
    2. Does the effective ignore list swallow files in it (and which ones)?
    3. Are there files that DO match a rule but are still ignored?
    4. Within a watch folder, which non-ignored files have no rule and can't
       be AI-classified (per the current AI extensions allow-list)?
    5. Which of the watch-excluded paths would have been organised if they
       were inside a watch folder? (bounded, dry-run only)
    6. What is the exact effective ignore list?

    Per-folder counts come from a bounded scan restricted to that folder, so
    the numbers are per-folder facts, not global totals repeated per folder.
    ``truncated`` flags a folder whose scan hit the cap instead of capping
    silently. The returned dict contains no secrets (config is redacted).
    """
    watch = config.resolved_watch_folders()
    out: dict[str, object] = {
        "config": config.to_dict(),  # redacted: no api_key in the JSON output
        "effective_ignore_patterns": config.effective_ignore_patterns(),
        "expanded_watch_folders": [str(p) for p in config.expanded_watch_folders()],
        "resolved_watch_folders": [str(p) for p in watch],
        "files": {},
        "excluded_paths": {},
    }

    for folder in watch:
        if not folder.is_dir():
            out["files"][str(folder)] = {"status": "missing"}
            continue
        from .mover import plan_actions

        actions = plan_actions(
            config,
            max_files=2000,
            include_unknown=False,
            folder=folder,
        )
        counts = actions["counts"]
        entries: dict[str, object] = {
            "status": "ok",
            "folder": str(folder),
            "truncated": actions["truncated"],
            "total_files": (
                counts["would_organize"]
                + counts["would_classify_ai"]
                + counts["would_skip_pattern"]
                + counts["would_skip_age"]
                + counts["would_skip_unknown"]
            ),
            "ignored_by_name_rule": counts["would_skip_pattern"],
            "no_rule_not_ai": counts["would_skip_unknown"],
            "would_organize": counts["would_organize"],
            "would_classify_ai": counts["would_classify_ai"],
            # Same source as the counts above (plan_actions' bounded scan),
            # so matched_rules honors config.recursive and can never disagree
            # with would_organize in the same payload (flaw #41). The deciding
            # category is reported verbatim — with sub_rules a file's category
            # may come from a sub-rule, not the global table, so deriving it
            # from the suffix via the table would misreport it.
            "matched_rules": sorted(
                r["category"]
                for r in actions["would_organize"]
                if r.get("category")
            ),
        }
        out["files"][str(folder)] = entries

    for path in watch_files:
        full = Path(path).expanduser()
        if not full.exists():
            out["excluded_paths"][str(full)] = {"exists": False}
            continue
        out["excluded_paths"][str(full)] = {
            "exists": True,
            "ignored": is_ignored_effective(full, config),
            "kind": "file" if full.is_file() else "folder",
        }
    return out


def render_watch_diag(data: dict[str, object]) -> str:
    """Human-readable rendering of :func:`watch_diag` output."""
    lines: list[str] = []
    add = lines.append
    add(f"  effective ignore patterns ({len(data['effective_ignore_patterns'])}):")
    for pat in data["effective_ignore_patterns"]:
        add(f"    {pat}")
    for folder, info in data["files"].items():
        add(f"\nWatched folder: {folder}  [{info['status']}]")
        add(
            f"  total files: {info.get('total_files', 0)}"
            + (" (truncated)" if info.get("truncated") else "")
        )
        add(f"  matched rules: {info.get('matched_rules', [])}")
        add(f"  ignored-by-name-rule: {info.get('ignored_by_name_rule', 0)}")
        add(f"  no_rule_not_ai: {info.get('no_rule_not_ai', 0)}")
        add(f"  would_organize: {info.get('would_organize', 0)}")
        add(f"  would_classify_ai: {info.get('would_classify_ai', 0)}")

    if data.get("excluded_paths"):
        add(f"\nWatch-excluded paths ({len(data['excluded_paths'])}):")
        for path, info in data["excluded_paths"].items():
            add(
                f"  {path}  exists={info.get('exists')}  "
                f"ignored={info.get('ignored')}  kind={info.get('kind')}"
            )
    return "\n".join(lines)
