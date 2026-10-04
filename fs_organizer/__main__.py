"""CLI entrypoint for fs-organizer."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .ai import classify_with_ai
from .config import DEFAULT_TARGET_RULES, ConfigError, load_config
from .diagnostics import (
    check_report,
    is_ignored_effective,
    render_watch_diag,
    watch_diag,
)
from .duplicates import (
    find_duplicates,
    quarantine_duplicates,
    render_duplicates,
    render_quarantine,
)
from .journal import export_journal
from .mover import _is_inside, _iter_files, age_policy_allows, move_file
from .rules import is_ignored, normalize_path_key
from .suggest import render_suggestions, suggestions
from .undo import render_undo, undo
from .watcher import Watcher


def _print(*args, **kwargs) -> None:
    """Print that never crashes under pythonw (no console available)."""
    with contextlib.suppress(Exception):  # pythonw: sys.stdout can be None
        print(*args, **kwargs)


def _build_log_handlers(
    log_file: str | None,
    max_bytes: int = 1_000_000,
    backup_count: int = 2,
) -> list[logging.Handler]:
    """Console handler (when a console exists) + rotating file handler."""
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if log_file:
        log_path = Path(os.path.expanduser(log_file))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=max_bytes,        # ~1 MB per file by default
                backupCount=backup_count,  # keep <name>.1 and <name>.2
                encoding="utf-8",
            )
        )
    return handlers


def _configure_logging(verbose: bool, log_file: str | None) -> None:
    """Reconfigurable logging setup (force=True so repeat calls work)."""
    handlers = _build_log_handlers(log_file)
    if not handlers:  # pythonw with no --log-file: drop everything silently
        logging.disable(logging.CRITICAL)
        return
    logging.disable(logging.NOTSET)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _inside_target_root(path: Path, config) -> bool:
    """Loop guard for --once: never re-process the organizer's own output.

    Uses the form-tolerant containment check — Path.resolve() can hand back
    Windows extended-path form ('\\\\?\\C:\\...'), which raw relative_to
    cannot match against a plain-form root.
    """
    return _is_inside(path, config.resolved_target_root())


def _one_shot(config, activity=None) -> int:
    """Scan watch folders once, organize everything, then exit.

    Walks the same scope the live watcher watches: the top level of every
    watch folder, plus all subfolders when ``config.recursive``. Returns the
    number of files moved (0 in dry-run mode); in dry-run the count of
    would-be moves is printed but 0 is returned so callers (UI) never
    mistake a preview for real work.
    """
    moved = 0
    would_move = 0
    for folder in config.resolved_watch_folders():
        if not folder.is_dir():
            _print(f"Skipping missing folder: {folder}")
            continue
        for path in _iter_files(folder, config.recursive):
            if _inside_target_root(path, config):
                continue  # already-organized output; never touch it again
            if is_ignored(path, config.effective_ignore_patterns()):
                continue
            if not path.is_file():
                continue  # _iter_files yields files, but stay defensive
            if not age_policy_allows(path, config):
                continue  # V2 age policy: too new/old — leave in place
            category = config.category_for(path)  # sub-rules first, then table
            if (
                category is None
                and config.ai.enabled
                and path.suffix.lower() in config.ai.extensions
            ):
                category = classify_with_ai(path, config.ai)
            if category and path.is_file():
                result = move_file(path, category, config)
                if activity is not None and (result.moved or result.would_move):
                    kind = "moved" if result.moved else "dryrun"
                    activity.add(
                        kind, path, f"-> {result.destination}",
                        display_name=result.destination.name,
                    )
                if result.moved:
                    moved += 1
                elif result.would_move:
                    would_move += 1
    if config.dry_run:
        _print(f"Dry run: would organize {would_move} file(s).")
    else:
        _print(f"Organized {moved} file(s).")
    return moved


def organize_paths(
    config,
    paths: list[str],
    recursive: bool = False,
    activity=None,
) -> dict[str, int]:
    """Organize explicitly named paths, even outside the watch folders.

    The on-demand counterpart to ``--once``: the user points at specific
    files/folders and they get the same treatment the watcher would give
    them. Reach is per-invocation and explicit -- nothing is watched, and no
    new folder is enrolled in the daemon.

    Every safety decision is delegated to ``move_file`` rather than
    reimplemented here, which is the whole point: the ignore list, the age
    policy, the target-root traversal guard, the collision-suffixing
    no-overwrite rule and the locked/vanished handling all live in one place,
    so a path organized on demand cannot take a different (weaker) route
    than the same file arriving through the watcher.

    What this function adds is *scope*, not permission: the scope guard
    (invariant 2, "never act outside the watched roots") is deliberately a
    property of the WATCHER, not of ``move_file``. Here the user has named
    the path explicitly, which is the authorization.

    Two guards are still enforced even on demand:

    - **Target-root loop guard.** A path already inside the organized
      subtree is refused, exactly as the watcher refuses it. Without this,
      ``organize ~/Organized`` would re-file its own output forever
      (``Documents/Documents/Documents/...``), each level a fresh move.
    - **Destination containment.** The final destination must land inside
      the target root; a category or template that escapes it is refused
      (``move_file`` owns that check).

    *paths* may be files or folders. A folder contributes its files the same
    way the watcher sees them: top level only, or the whole subtree when
    *recursive* is true. Missing paths are reported and skipped rather than
    raising -- one bad argument must not abort the rest.

    Returns a counter dict; raises nothing for routine per-file problems.
    """
    counts = {"moved": 0, "would_move": 0, "skipped": 0, "refused": 0, "unmatched": 0}
    target_root = config.resolved_target_root()

    # Expand each argument to concrete candidate files, preserving order and
    # dropping duplicates so an overlapping folder+file argument can't move
    # the same file twice.
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        full = Path(raw).expanduser()
        try:
            if full.is_dir():
                for path in _iter_files(full, recursive):
                    # Canonical key: the same file named twice (two spellings
                    # of one path) must be organized once, not twice.
                    key = normalize_path_key(path)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(path)
            elif full.is_file():
                key = normalize_path_key(full)
                if key not in seen:
                    seen.add(key)
                    candidates.append(full)
            else:
                counts["skipped"] += 1
                _print(f"Skipping missing path: {full}")
        except OSError as exc:
            counts["skipped"] += 1
            _print(f"Skipping unreadable path {full}: {exc}")

    for path in candidates:
        # Loop guard: never organize our own output, on demand either.
        if _is_inside(path, target_root):
            counts["skipped"] += 1
            continue
        category = config.category_for(path)  # sub-rules first, then table
        if (
            category is None
            and config.ai.enabled
            and path.suffix.lower() in config.ai.extensions
        ):
            category = classify_with_ai(path, config.ai)
        if not category:
            # No rule and not AI-able: leave it exactly where it is.
            counts["unmatched"] += 1
            continue
        result = move_file(path, category, config)
        if activity is not None and (result.moved or result.would_move):
            kind = "moved" if result.moved else "dryrun"
            activity.add(
                kind, path, f"-> {result.destination}",
                display_name=result.destination.name,
            )
        if result.refused:
            counts["refused"] += 1
        elif result.moved:
            counts["moved"] += 1
        elif result.would_move:
            counts["would_move"] += 1
        else:
            counts["skipped"] += 1
    return counts


def _cmd_organize(args, config=None) -> int:
    """Implement the organize command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2

    # --dry-run on the command line overrides the config for this run only.
    effective = config
    if args.dry_run and not config.dry_run:
        effective = replace(config, dry_run=True)

    counts = organize_paths(
        effective,
        args.paths,
        recursive=args.recursive,
    )
    _print(f"Config: {args.config}")
    _print(
        "Moved {moved}, would move {would_move}, skipped {skipped}, "
        "refused {refused}, no matching rule {unmatched}.".format(**counts)
    )
    if counts["refused"]:
        # A refusal is a policy decision, not a hiccup: surface it loudly so
        # it can't hide inside a summary line.
        _print("Note: some paths were REFUSED by policy and were not moved.")
    return 0


def _wait_forever(stop: threading.Event) -> None:
    # Poll in short slices: on Windows a console signal (Ctrl+C / CTRL_BREAK)
    # CANNOT interrupt an in-progress Event.wait — the KeyboardInterrupt only
    # runs when the wait returns, so a long timeout would leave the process
    # unkillable-by-signal for up to that long (verified empirically; the e2e
    # smoke script scripts/smoke_dryrun.py guards this).
    while not stop.wait(timeout=0.5):
        pass


def _cmd_check(args, config=None) -> int:
    """Implement the check command; returns the CLI exit code.

    ``config`` is the already-loaded, already-validated Config from
    ``main()``; it is only loaded here when the command is invoked directly
    (tests, embedded use), so a normal run never parses the file twice.
    """
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2

    # --watch-files prints whether each supplied path is ignored by the
    # *effective* ignore list. This is a plain verification surface — the
    # path is NOT walked, so it can safely point at a target-root file or an
    # external folder. The ignore verdict is the entire point of the flag.
    ignore_check = {
        str(Path(p).expanduser().resolve()): is_ignored_effective(Path(p).expanduser(), config)
        for p in args.watch_files
    }

    if args.json:
        payload = config.to_dict()  # api_key is redacted
        if args.watch_files:
            payload["ignore_check"] = ignore_check
        _print(json.dumps(payload, indent=2, default=str))
        return 0

    _print(f"Config: {args.config}")
    _print(check_report(config, ignore_check))
    return 0


def _build_check_parser(subparsers) -> None:
    """``fs-organizer CONFIG check`` -- validate a config file and print the
    fully resolved view a live run would use (rules, ignore list, AI,
    target root, coverage gaps). Exits 0 on a healthy config, 2 on the
    same errors ``main()`` returns.
    """
    parser = subparsers.add_parser(
        "check",
        help="Validate a config file and print the resolved view it defines",
        description=(
            "Load ``CONFIG`` exactly the way the organizer does (expanduser, "
            "resolve, normalise rules, build the effective ignore list and "
            "AI policy) and print what it actually sees. Good for "
            "debugging why a file is not being moved."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full resolved config as JSON instead of the human summary",
    )
    parser.set_defaults(func=_cmd_check)


def _build_watch_dbg_parser(subparsers) -> None:
    """``fs-organizer CONFIG watch-diag`` -- print the ignore decisions and
    rule matching for every file under the configured watch folders.
    """
    parser = subparsers.add_parser(
        "watch-diag",
        help="Print ignore decisions and rule matching for every file under the watch folders",
        description=(
            "For each watch folder, run the same bounded dry-scan the "
            "Organizer uses, then print per-folder: number ignored by the "
            "effective ignore list (and how many of those match a rule), "
            "number with no rule and not AI-classifiable, plus the exact "
            "effective ignore patterns. Add ``--watch-files`` to also see the "
            "ignore verdict for any path outside the watch folders."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the diagnostic as JSON instead of the human summary",
    )
    parser.set_defaults(func=_cmd_watch_diag)


def _cmd_watch_diag(args, config=None) -> int:
    """Implement the watch-diag command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2

    data = watch_diag(config, args.watch_files)

    if args.json:
        _print(json.dumps(data, indent=2, default=str))
        return 0

    _print(f"Config: {args.config}")
    _print(render_watch_diag(data))
    return 0


def _build_organize_parser(subparsers) -> None:
    """``fs-organizer CONFIG organize PATH...`` -- organize specific paths.

    The on-demand escape hatch: act on files the user names explicitly,
    including ones outside every watch folder. Reach is per-invocation --
    nothing is added to the watcher.
    """
    parser = subparsers.add_parser(
        "organize",
        help="Organize explicitly named files/folders (works outside the watch folders)",
        description=(
            "Organize the given paths on demand, even when they sit outside "
            "every watch folder. Each argument may be a file or a folder; a "
            "folder contributes its top-level files, or the whole subtree "
            "with --recursive. Files already inside the target root are "
            "never touched (they are this tool's own output). All the usual "
            "rules still apply: ignore patterns, age policy, category "
            "containment, and never overwriting an existing file."
        ),
    )
    parser.add_argument(
        "paths", nargs="+", metavar="PATH",
        help="One or more files or folders to organize.",
    )
    parser.add_argument(
        "--recursive", action="store_true",
        help="For folder arguments, include subfolders (default: top level only).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen; move nothing (overrides the config for this run).",
    )
    parser.set_defaults(func=_cmd_organize)


def _build_dupes_parser(subparsers) -> None:
    """``fs-organizer CONFIG dupes`` -- report duplicate files under the watch
    folders. Report-only by design: nothing is ever moved or deleted; the
    scan (size pre-filter + streamed SHA-256) is bounded so it cannot turn
    into a disk-thrashing job.
    """
    parser = subparsers.add_parser(
        "dupes",
        help="Report duplicate files under the watch folders (report-only)",
        description=(
            "Find duplicate files by size pre-filter plus streamed SHA-256 "
            "content hash, over exactly the scope the organizer watches. "
            "Report-only: nothing is moved, renamed, or deleted. Files whose "
            "byte size is unique are never read at all."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the duplicate report as JSON instead of the human summary",
    )
    parser.add_argument(
        "--max-files", type=int, default=2000,
        help="Cap on files examined per scan (default: 2000)",
    )
    parser.add_argument(
        "--max-mb", type=int, default=256,
        help="Total content bytes to hash per scan, in MiB (default: 256)",
    )
    parser.add_argument(
        "--min-size", type=int, default=1,
        help="Ignore files smaller than this many bytes (default: 1)",
    )
    parser.set_defaults(func=_cmd_dupes)


def _cmd_dupes(args, config=None) -> int:
    """Implement the dupes command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2

    data = find_duplicates(
        config,
        max_files=max(1, args.max_files),
        max_bytes_hashed=max(0, args.max_mb) * 1024 * 1024,
        min_size=max(0, args.min_size),
    )
    if args.json:
        _print(json.dumps(data, indent=2, default=str))
        return 0
    _print(f"Config: {args.config}")
    _print(render_duplicates(data))
    return 0


def _build_undo_parser(subparsers) -> None:
    """``fs-organizer CONFIG undo [N]`` -- restore the last N organized files."""
    parser = subparsers.add_parser(
        "undo",
        help="Restore the most recently organized files to their original locations",
        description=(
            "Read the move journal and move files back where they came from. "
            "Never overwrites (a re-occupied original gets a (1) suffix) and "
            "never deletes anything. Requires the journal to be enabled."
        ),
    )
    parser.add_argument(
        "count", nargs="?", type=int, default=1, metavar="N",
        help="How many of the most recent moves to undo (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be restored; move nothing",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=_cmd_undo)


def _cmd_undo(args, config=None) -> int:
    """Implement the undo command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2
    if not config.journal.enabled:
        _print("Journal is disabled — nothing to undo (set journal.enabled to record moves).")
        return 1
    effective = replace(config, dry_run=True) if args.dry_run else config
    results = undo(effective, count=max(0, args.count))
    if args.json:
        _print(json.dumps(
            [
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
            indent=2, default=str,
        ))
        return 0
    _print(f"Config: {args.config}")
    _print(render_undo(results, dry_run=args.dry_run))
    return 0


def _build_suggest_parser(subparsers) -> None:
    """``fs-organizer CONFIG suggest`` -- propose rules from what was skipped."""
    parser = subparsers.add_parser(
        "suggest",
        help="Suggest new rules based on files that match nothing",
        description=(
            "Scan the watch folders for files with no matching rule and no AI "
            "coverage, and propose config snippets to handle them. Also "
            "summarizes journal activity per category."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument(
        "--max-files", type=int, default=2000,
        help="Cap on files examined per scan (default: 2000)",
    )
    parser.set_defaults(func=_cmd_suggest)


def _cmd_suggest(args, config=None) -> int:
    """Implement the suggest command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2
    data = suggestions(config, max_files=max(1, args.max_files))
    if args.json:
        _print(json.dumps(data, indent=2, default=str))
        return 0
    _print(f"Config: {args.config}")
    _print(render_suggestions(data))
    return 0


def _build_quarantine_parser(subparsers) -> None:
    """``fs-organizer CONFIG quarantine`` -- move duplicate copies aside."""
    parser = subparsers.add_parser(
        "quarantine",
        help="Move duplicate copies into a quarantine folder (never deletes)",
        description=(
            "Run the duplicate scan, then move every copy except the oldest "
            "of each group into a quarantine folder for review. Nothing is "
            "ever deleted; the move is reversible."
        ),
    )
    parser.add_argument(
        "--dest", default=None,
        help="Quarantine root (default: <target_root>/_Duplicates)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.set_defaults(func=_cmd_quarantine)


def _cmd_quarantine(args, config=None) -> int:
    """Implement the quarantine command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2
    report = find_duplicates(config)
    root = (
        Path(os.path.expanduser(args.dest))
        if args.dest
        else config.resolved_target_root() / "_Duplicates"
    )
    dry_run = args.dry_run or config.dry_run
    data = quarantine_duplicates(config, report, root, dry_run=dry_run)
    if args.json:
        _print(json.dumps(data, indent=2, default=str))
        return 0
    _print(f"Config: {args.config}")
    _print(render_quarantine(data))
    return 0


def _build_export_parser(subparsers) -> None:
    """``fs-organizer CONFIG export`` -- dump activity as CSV or JSON."""
    parser = subparsers.add_parser(
        "export",
        help="Export the move journal as CSV or JSON",
        description="Dump the journal's move history for external analysis.",
    )
    parser.add_argument(
        "--format", choices=["csv", "json"], default="csv",
        help="Output format (default: csv)",
    )
    parser.set_defaults(func=_cmd_export)


def _cmd_export(args, config=None) -> int:
    """Implement the export command; returns the CLI exit code."""
    if config is None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            _print(f"Config error: {exc}")
            return 2
    data = export_journal(config, fmt=args.format)
    _print(data, end="" if data.endswith("\n") else "\n")
    return 0


def _build_init_parser(subparsers) -> None:
    """``fs-organizer init`` -- write a starter config file."""
    parser = subparsers.add_parser(
        "init",
        help="Write a starter config file watching common user folders",
        description=(
            "Create a minimal, valid config for the folders that exist in your "
            "home directory (Desktop, Documents, Downloads, ...). Refuses to "
            "overwrite an existing file unless --force is given."
        ),
    )
    parser.add_argument(
        "--output", default=None, help="Where to write (default: ./config.json)"
    )
    parser.add_argument("--force", action="store_true", help="Overwrite an existing file")
    parser.set_defaults(func=_cmd_init)


def _cmd_init(args, config=None) -> int:
    """Implement the init command; returns the CLI exit code.

    ``config`` is unused (there is nothing to load yet) — the attribute is
    accepted so the dispatch table can call every command the same way.
    """
    # ``init`` creates a config file, so it runs before the config load in
    # main(). The target defaults to the path the CLI was handed (which need
    # not exist yet); --output overrides it. Either way, ``init`` writes the
    # file you then pass to every other command.
    out = Path(os.path.expanduser(args.output or args.config))
    if out.exists() and not args.force:
        _print(f"Refusing to overwrite existing {out} (pass --force to replace it).")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    candidates = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"]
    folders = [str(home / name) for name in candidates if (home / name).is_dir()]
    if not folders:
        folders = [str(home)]
    payload = {
        "watch_folders": folders,
        "target_rules": dict(DEFAULT_TARGET_RULES),
        "ignore_patterns": [".*", "*.tmp", "*.part", "*.crdownload", "*~", "desktop.ini", "Thumbs.db"],
        "target_root": str(home / "Organized"),
        "use_date_subfolders": False,
        "dry_run": True,
        "file_stable_seconds": 1.0,
        "recursive": False,
        "journal": {"enabled": True, "path": None},
        "ai": {"enabled": False},
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _print(f"Wrote {out} (dry_run is ON — set it to false when you are ready).")
    _print(f"Watching {len(folders)} folder(s): {folders}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Shared flag group (available on the main parser and all subcommands).
    # argparse cannot merge a parent positional into a subparser, so the
    # positional stays on the main parser; every other flag is declared here
    # and inherited by each subcommand via the ``parents`` argument.
    common_flags = argparse.ArgumentParser(add_help=False)
    common_flags.add_argument(
        "--once", action="store_true",
        help="Organize existing files once and exit (no watching)"
    )
    common_flags.add_argument(
        "--watch-files", metavar="PATH", action="append", default=[],
        help=(
            "One path (file or folder) per flag. Print whether it is ignored "
            "by the effective ignore list. Works with ``check`` and "
            "``watch-diag``; the path is NOT walked, so it can safely point "
            "at a target-root file or an external folder."
        ),
    )
    common_flags.add_argument(
        "--verbose", "-v", action="store_true",
        help="Debug logging",
    )
    common_flags.add_argument(
        "--log-file", default=None,
        help="Also write logs to this file (default: none)",
    )
    common_flags.add_argument(
        "--ui", action="store_true",
        help="Serve a local web dashboard (http://127.0.0.1:PORT) while watching",
    )
    common_flags.add_argument(
        "--port", type=int, default=8765,
        help="Port for the web dashboard (default: 8765; 0 picks a free port)",
    )
    common_flags.add_argument(
        "--no-browser", action="store_true",
        help="With --ui: do not open the browser automatically",
    )
    common_flags.add_argument(
        "--force", action="store_true",
        help="Start even if another daemon instance holds this config's lock",
    )
    # Commands reuse the ``config`` positional from the main parser. The
    # positional must be declared before the subparsers so argparse can match
    # them (a subparser cannot consume a positional from the parent parser).
    # The CLI order is:
    #   fs-organizer CONFIG check
    #   fs-organizer CONFIG watch-diag
    #   fs-organizer CONFIG --once
    parser = argparse.ArgumentParser(
        prog="fs-organizer",
        description="Rule-based file organizer with optional AI fallback.",
        parents=[common_flags],
    )
    parser.add_argument(
        "config", help="Path to JSON config file"
    )
    sub = parser.add_subparsers(dest="subcommand", help="Subcommands")
    _build_check_parser(sub)
    _build_watch_dbg_parser(sub)
    _build_dupes_parser(sub)
    _build_organize_parser(sub)
    _build_undo_parser(sub)
    _build_suggest_parser(sub)
    _build_quarantine_parser(sub)
    _build_export_parser(sub)
    _build_init_parser(sub)
    args = parser.parse_args(argv)

    # Subcommands consume the positional and their own flags, so default any
    # attributes the main parser defines but the subcommand may not set.
    for attr in ("verbose", "log_file", "port", "no_browser"):
        if not hasattr(args, attr):
            setattr(args, attr, None if attr != "port" else 8765)
    for attr in ("once", "ui"):
        if not hasattr(args, attr):
            setattr(args, attr, False)

    _configure_logging(args.verbose, getattr(args, "log_file", None))

    if args.port < 0 or args.port > 65535:
        parser.error(f"--port must be 0-65535, got {args.port}")

    # ``init`` writes a config file that does not exist yet, so it must run
    # before the load below (every other command validates the file first).
    if args.subcommand == "init":
        return _cmd_init(args, None)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print(f"Config error: {exc}")
        return 2

    # main() already loaded and validated the config above — hand it to the
    # subcommand instead of parsing the file a second time.
    if args.subcommand == "check":
        return _cmd_check(args, config)
    if args.subcommand == "watch-diag":
        return _cmd_watch_diag(args, config)
    if args.subcommand == "dupes":
        return _cmd_dupes(args, config)
    if args.subcommand == "organize":
        return _cmd_organize(args, config)
    if args.subcommand == "undo":
        return _cmd_undo(args, config)
    if args.subcommand == "suggest":
        return _cmd_suggest(args, config)
    if args.subcommand == "quarantine":
        return _cmd_quarantine(args, config)
    if args.subcommand == "export":
        return _cmd_export(args, config)

    if args.once:
        # One-shot mode: organize once and exit. We skip the daemon loop and
        # the UI entirely.
        if args.ui:
            _print("Note: --ui is ignored together with --once (one-shot exits immediately).")
        _one_shot(config)
        return 0

    # V2 single-instance guard: one daemon per config file (resolved), so two
    # organizers can never race the same folders. Different configs may run
    # side by side. --force overrides deliberately.
    from .runtime import InstanceLock

    lock_path = Path(os.path.expanduser(args.config)).resolve().with_suffix(".lock")
    instance_lock = InstanceLock(lock_path)
    ok, lock_msg = instance_lock.acquire(force=getattr(args, "force", False))
    if not ok:
        _print(f"Not starting: {lock_msg}")
        return 3

    # Optional live dashboard (see ui.py); shares the activity feed with the
    # watcher so the UI reflects what the organizer actually did.
    activity = None
    dashboard = None
    if args.ui:
        from .ui import ActivityLog, Dashboard

        activity = ActivityLog()
        # The dashboard gets the watcher + config path so pause/reload work.
        # The watcher must exist first; construct it now, start after wiring.
        watcher = Watcher(config, activity=activity)
        dashboard = Dashboard(
            config, activity, port=args.port, open_browser=not args.no_browser,
            watcher=watcher, config_path=args.config,
        )
        try:
            dashboard.start()
        except (OSError, RuntimeError) as exc:
            _print(f"Cannot start dashboard on port {dashboard.port}: {exc}")
            _print("Continuing without the UI (try --port 0 to pick a free port).")
            dashboard = None
            activity = None
    else:
        watcher = Watcher(config, activity=None)

    watcher.start()
    _print(f"fs-organizer running. Watching {len(config.resolved_watch_folders())} folder(s). Ctrl+C to stop.")
    stop = threading.Event()
    try:
        _wait_forever(stop)
    except KeyboardInterrupt:
        _print("\nStopping...")
    finally:
        watcher.stop()
        if dashboard is not None:
            dashboard.stop()
        instance_lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
