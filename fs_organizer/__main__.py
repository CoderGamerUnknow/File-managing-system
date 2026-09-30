"""CLI entrypoint for fs-organizer."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from .config import ConfigError, load_config
from .diagnostics import check_report, is_ignored_effective, render_watch_diag
from .mover import _is_inside, _iter_files, move_file
from .rules import is_ignored, match_extension
from .watcher import Watcher


def _print(*args, **kwargs) -> None:
    """Print that never crashes under pythonw (no console available)."""
    try:
        print(*args, **kwargs)
    except Exception:  # noqa: BLE001 - no console
        pass


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
            category = match_extension(path, config.target_rules)
            if category is None and config.ai.enabled:
                if path.suffix.lower() in config.ai.extensions:
                    from .ai import classify_with_ai

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


def _wait_forever(stop: threading.Event) -> None:
    # Poll in short slices: on Windows a console signal (Ctrl+C / CTRL_BREAK)
    # CANNOT interrupt an in-progress Event.wait — the KeyboardInterrupt only
    # runs when the wait returns, so a long timeout would leave the process
    # unkillable-by-signal for up to that long (verified empirically; the e2e
    # smoke script scripts/smoke_dryrun.py guards this).
    while not stop.wait(timeout=0.5):
        pass


def _cmd_check(args) -> int:
    """Implement the check command; returns the CLI exit code."""
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


def _cmd_watch_diag(args) -> int:
    """Implement the watch-diag command; returns the CLI exit code."""
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print(f"Config error: {exc}")
        return 2

    data = _watch_diag(config, args.watch_files)

    if args.json:
        _print(json.dumps(data, indent=2, default=str))
        return 0

    _print(f"Config: {args.config}")
    _print(render_watch_diag(data))
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

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _print(f"Config error: {exc}")
        return 2

    if args.subcommand == "check":
        return _cmd_check(args)
    if args.subcommand == "watch-diag":
        return _cmd_watch_diag(args)

    if args.once:
        # One-shot mode: organize once and exit. We skip the daemon loop and
        # the UI entirely.
        if args.ui:
            _print("Note: --ui is ignored together with --once (one-shot exits immediately).")
        _one_shot(config)
        return 0

    # Optional live dashboard (see ui.py); shares the activity feed with the
    # watcher so the UI reflects what the organizer actually did.
    activity = None
    dashboard = None
    if args.ui:
        from .ui import ActivityLog, Dashboard

        activity = ActivityLog()
        dashboard = Dashboard(
            config, activity, port=args.port, open_browser=not args.no_browser
        )
        try:
            dashboard.start()
        except (OSError, RuntimeError) as exc:
            _print(f"Cannot start dashboard on port {dashboard.port}: {exc}")
            _print("Continuing without the UI (try --port 0 to pick a free port).")
            dashboard = None
            activity = None

    watcher = Watcher(config, activity=activity)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
