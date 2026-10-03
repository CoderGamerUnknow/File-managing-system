"""File moving logic: destination resolution, collision handling, dry-run support.

Correctness notes:

* Every watch-folder scan is bounded by ``max_files`` and covers exactly the
  paths the live watcher organizes: the top level of each watch folder
  (``iterdir``), plus every subfolder when ``recursive`` is enabled. Plans,
  diagnostics, and the dashboard therefore never advertise work the watcher
  will not do.
* ``move_file`` never raises for routine problems; it returns a
  ``MoveResult`` (``moved``, ``would_move``, ``skipped``, ``transient``, or
  ``refused``).
* Cross-volume moves are staged (copy → fsync → rename → delete) so a crash
  mid-copy cannot leave a partial file next to an intact source. Every
  successful move is recorded in the move journal (see journal.py) when
  journaling is enabled.
* One lock per *destination directory* serializes the existence-check +
  move sequence. Two workers moving same-named files from different watch
  folders can otherwise both pass the check and the second
  ``shutil.move`` silently overwrites the first (verified data loss).
* The ignore decision always uses the *effective* ignore list (built-in
  defaults + user patterns) — the same list the diagnostics display.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import journal
from .config import Config
from .rules import is_ignored

__all__ = [
    "MoveResult",
    "age_policy_allows",
    "destination_for",
    "move_file",
    "plan",
    "plan_actions",
]

logger = logging.getLogger("fs_organizer")

# Serializes the existence-check + move sequence per destination directory.
# Without this, two workers moving same-named files from different watch
# folders can both pass the "is the name free?" check and the second
# shutil.move silently overwrites the first (verified data loss).
_dir_locks: dict[Path, threading.Lock] = {}
_dir_locks_guard = threading.Lock()

# Compound extensions kept whole when collision-suffixing: "a.tar.gz" becomes
# "a (1).tar.gz", not "a.tar (1).gz".
_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lzma")


def _lock_for(directory: Path) -> threading.Lock:
    with _dir_locks_guard:
        lock = _dir_locks.get(directory)
        if lock is None:
            lock = threading.Lock()
            _dir_locks[directory] = lock
        return lock


def _normalize_for_compare(p: Path) -> str:
    """Normalize a path for containment checks.

    ``Path.resolve()`` intermittently returns Windows extended-path form
    (``\\\\?\\C:\\...``) depending on what exists at resolve time, which made
    ``is_relative_to`` refuse destinations that were in fact inside the
    target root. Strip any repeated prefix (a caller may add one on top of
    an already-extended resolve) and fold case so the guard is stable.
    """
    s = str(p)
    while True:
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[8:]
        elif s.startswith("\\\\?\\"):
            s = s[4:]
        else:
            break
    return os.path.normcase(s)


def _is_inside(child: Path, root: Path) -> bool:
    """True if *child* equals or lives under *root* (prefix/case tolerant)."""
    c, r = _normalize_for_compare(child), _normalize_for_compare(root)
    return c == r or c.startswith(r + os.sep)


@dataclass
class MoveResult:
    """Outcome of a move attempt (dry-run counts as would_move).

    ``reason`` names why nothing happened ("ignored", "file vanished", ...)
    and ``refused`` marks a *policy* refusal — e.g. a category that would
    escape the target root — as distinct from a routine skip.
    """

    moved: bool = False        # file actually moved
    would_move: bool = False   # dry-run: move would happen
    skipped: bool = False      # no action taken
    transient: bool = False    # transiently locked (WinError 32); caller may retry
    refused: bool = False      # refused by policy (never a routine skip)
    reason: str = ""
    destination: Path | None = None


def _stem_suffix(dest: Path) -> tuple[str, str]:
    """Split a file name into (stem, extension), keeping compound extensions whole."""
    lowered = dest.name.lower()
    for compound in _COMPOUND_SUFFIXES:
        if lowered.endswith(compound):
            return dest.name[: -len(compound)], dest.name[-len(compound) :]
    return dest.stem, dest.suffix


def _unique_destination(dest: Path) -> Path:
    """Return a destination path that doesn't exist by appending (1), (2), ..."""
    if not dest.exists():
        return dest
    stem, suffix = _stem_suffix(dest)
    for i in range(1, 1000):
        candidate = dest.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    # Give up on uniquifying after 999 collisions.
    raise FileExistsError(f"Too many collisions for {dest}")


def destination_for(
    path: Path, category: str, config: Config, mtime: float | None = None
) -> Path:
    """Compute the destination directory for a file given its category.

    V2: ``destination_template`` drives the layout when set — the template
    renders tokens (``{category}``, ``{date:FORMAT}``) against the file's
    mtime and is resolved under the target root. The legacy
    ``use_date_subfolders`` flag keeps working (it is exactly the template
    ``{category}/{date:%Y-%m}``); the template wins if both are set.

    ``mtime``: callers that have already stat'ed the file pass it in so no
    second ``path.stat()`` happens — that second read was a TOCTOU race:
    the async scan can move the file between the two stats and the
    ``FileNotFoundError`` would escape ``plan()`` (HTTP 500 on /api/status).
    """
    root = config.resolved_target_root()
    if mtime is None:
        mtime = path.stat().st_mtime
    template = getattr(config, "destination_template", None)
    if template is not None and template.pattern != "{category}":
        rel = template.render(category, mtime)
        dest = root.joinpath(*rel.split("/"))
    elif getattr(config, "use_date_subfolders", False):
        dest = root / category / datetime.fromtimestamp(mtime).strftime("%Y-%m")
    else:
        dest = root / category
    return dest


def age_policy_allows(path: Path, config: Config, now: float | None = None) -> bool:
    """V2 age policy gate: True when the file may be organized now.

    Best-effort by design — a file that vanishes or an unreadable mtime
    allows the move (the rest of the pipeline re-checks existence anyway);
    failing closed here would turn a stat race into a stuck file.
    """
    policy = getattr(config, "age_policy", None)
    if policy is None:
        return True
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    if now is None:
        now = time.time()
    return not policy.violates(mtime, now)


def _iter_files(folder: Path, recursive: bool):
    """Yield candidate files under *folder* in the scope the watcher watches.

    Top level by default; every subfolder too when ``recursive`` (the
    watcher's opt-in). The target-root subtree is never descended into —
    organized output is not input. A folder that vanishes mid-walk just
    stops producing entries.
    """
    if not recursive:
        try:
            yield from (p for p in sorted(folder.iterdir()) if p.is_file())
        except OSError:
            return
        return
    stack: list[Path] = [folder]
    while stack:
        current = stack.pop(0)  # BFS keeps a stable folder-major order
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_dir():
                    stack.append(child)
                elif child.is_file():
                    yield child
            except OSError:
                continue


def _scan_files(
    config: Config, max_files: int, folder: Path | None = None
) -> tuple[list[dict], bool]:
    """Scan the watch folders exactly as the organizer sees them.

    Walks the scope the watcher watches — the top level of every existing
    watch folder, plus all subfolders when ``config.recursive`` — and
    classifies each file:

        {
            "path": Path,
            "category": str | None,      # from config.target_rules
            "destination": Path | None,  # only when category is not None
            "decision": "organize" | "ai" | "pattern" | "unknown",
        }

    ``folder`` restricts the scan to a single watch folder (watch-diag's
    per-folder stats); ``None`` scans all of them.

    Returns ``(entries, truncated)``. The entry list never exceeds
    ``max_files``; ``truncated`` is True when the folders held more.
    A file that vanishes mid-scan (or any OSError while stat-ing it) is
    dropped without killing the scan.
    """
    ignore_list = config.effective_ignore_patterns()
    target_root = config.resolved_target_root()
    ai_extensions = {e.lower() for e in config.ai.extensions}
    entries: list[dict] = []
    truncated = False

    folders = [folder] if folder is not None else config.resolved_watch_folders()
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in _iter_files(folder, config.recursive):
            if len(entries) >= max_files:
                truncated = True
                return entries, truncated
            if _is_inside(path, target_root):
                continue  # already-organised output, never an input
            if is_ignored(path, ignore_list):
                decision = "pattern"
                category = destination = None
            else:
                category = config.category_for(path)  # sub-rules first, then table
                if category is not None:
                    if not age_policy_allows(path, config):
                        # V2 age policy: matchable, but outside the age
                        # window — reported as its own decision so plans
                        # never advertise a move the organizer won't make.
                        decision = "age"
                        category = destination = None
                    else:
                        try:
                            dest_dir = destination_for(path, category, config)
                        except OSError:
                            continue  # vanished between is_file() and stat()
                        decision = "organize"
                        destination = dest_dir / path.name
                elif path.suffix.lower() in ai_extensions:
                    decision = "ai"
                    category = destination = None
                else:
                    decision = "unknown"
                    category = destination = None
            entries.append(
                {
                    "path": path,
                    "category": category,
                    "destination": destination,
                    "decision": decision,
                }
            )
    return entries, truncated


def _plan_row(path: Path, category: str, config: Config, stat: os.stat_result) -> dict:
    """Build a single plan row for *path*.

    ``stat`` is passed in so callers never need a second ``path.stat()`` —
    the caller has already guarded against ``OSError`` (a file that vanished
    between the scan and the read).

    ``category`` is reported verbatim — the rule/AI category that decided
    the plan. Deriving it from ``dest_dir.name`` would report the ``YYYY-MM``
    month folder as the category when ``use_date_subfolders`` is on
    (flaw #43, same class as the journal's #40).
    """
    # Pass the already-read mtime: re-statting here re-introduced the race
    # where the async scan moves the file between plan()'s stat and this one
    # (FileNotFoundError escaped plan() and took /api/status down with it).
    dest_dir = destination_for(path, category, config, mtime=stat.st_mtime)
    return {
        "name": path.name,
        "category": category,
        "destination": dest_dir / path.name,
        "size": stat.st_size,
        "would_move": False,
    }


def plan(config: Config, max_files: int = 2000) -> dict:
    """Dry-run *pre-scan*: list every file that would be organized, with its
    destination and size, without touching the filesystem.

    Each row is a dict with keys ``name, category, destination, size,
    would_move(false)``. ``truncated: true`` means the watch folders held
    more candidates than ``max_files``. A file that vanished between the
    scan and its ``stat()`` is dropped; its absence never raises.
    """
    rows: list[dict] = []
    scan_entries, truncated = _scan_files(config, max_files)
    for entry in scan_entries:
        if entry["decision"] != "organize":
            continue
        try:
            stat = entry["path"].stat()
        except OSError:
            continue
        rows.append(_plan_row(entry["path"], entry["category"], config, stat))
    return {
        "target_root": str(config.resolved_target_root()),
        "watch_folders": [str(w) for w in config.resolved_watch_folders()],
        "total": len(rows),
        "truncated": truncated,
        "rows": rows,
        "would_move": len(rows),
    }


def plan_actions(
    config: Config,
    max_files: int = 2000,
    include_unknown: bool = False,
    folder: Path | None = None,
) -> dict:
    """A higher-level dry-run report: what will happen to each file, and why.

    Groups every candidate under a watch folder into one of:

    - ``would_organize``: an exact extension rule matched (guaranteed path)
    - ``would_classify_ai``: no rule, but the extension is in the AI allow-list
    - ``would_skip_pattern``: ignored by the effective ignore list
    - ``would_skip_unknown``: no rule and the extension is not AI-able (left
      alone by design)

    Use it to answer "why didn't that file move?" before the first real run.
    With ``include_unknown`` the ``would_skip_unknown`` bucket is merged into
    ``would_organize`` (an "attempt AI for everything" policy preview).

    The returned lists and counts are bounded by ``max_files``; the scan
    stops producing entries once the cap is reached.
    """
    out = {
        "target_root": str(config.resolved_target_root()),
        "watch_folders": [str(w) for w in config.resolved_watch_folders()],
        "truncated": False,
        "counts": {
            "would_organize": 0,
            "would_classify_ai": 0,
            "would_skip_pattern": 0,
            "would_skip_age": 0,
            "would_skip_unknown": 0,
        },
        "would_organize": [],
        "would_classify_ai": [],
        "would_skip_pattern": [],
        "would_skip_age": [],
        "would_skip_unknown": [],
    }

    scan_entries, truncated = _scan_files(config, max_files, folder=folder)
    out["truncated"] = truncated
    for entry in scan_entries:
        bucket = {
            "organize": "would_organize",
            "ai": "would_classify_ai",
            "pattern": "would_skip_pattern",
            "age": "would_skip_age",
            "unknown": "would_skip_unknown",
        }[entry["decision"]]
        out["counts"][bucket] += 1
        if bucket == "would_organize":
            out["would_organize"].append(
                {
                    "path": entry["path"],
                    "category": entry["category"],
                    "destination": entry["destination"],
                }
            )
        elif bucket == "ai":
            out["would_classify_ai"].append(
                {"path": entry["path"], "extension": entry["path"].suffix or "(none)"}
            )
        elif bucket == "pattern":
            out["would_skip_pattern"].append({"path": entry["path"], "reason": "ignored"})
        elif bucket == "would_skip_age":
            out["would_skip_age"].append({"path": entry["path"], "reason": "outside age policy"})
        else:
            out["would_skip_unknown"].append({"path": entry["path"], "reason": "no rule, not AI-able"})

    if include_unknown:
        out["counts"]["would_organize"] += out["counts"]["would_skip_unknown"]
        out["would_organize"] += out["would_skip_unknown"]
        out["would_skip_unknown"] = []

    return out


def _same_volume(a: Path, b: Path) -> bool:
    """True when both paths sit on the same Windows drive ("C:" prefix)."""
    return os.path.splitdrive(str(a))[0].lower() == os.path.splitdrive(str(b))[0].lower()


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so a rename survives power loss (POSIX).

    Windows refuses to open directories (PermissionError) and does not need
    this — os.replace there is already crash-atomic. Never raises.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Streamed SHA-256 of a file (constant memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verify_copy(source: Path, copy: Path) -> bool:
    """True when *copy* has byte-identical content to *source*.

    Size is compared first (cheap, catches the common truncation) and the
    streamed hash only runs when the sizes agree, so the extra read is paid
    only on the cross-volume path that already read both files once.
    """
    try:
        if source.stat().st_size != copy.stat().st_size:
            return False
        return _hash_file(source) == _hash_file(copy)
    except OSError:
        return False


def _free_percent(path: Path) -> float:
    """Free-space percentage of the volume holding *path* (-1.0 if unknown)."""
    try:
        usage = shutil.disk_usage(str(path))
        return 100.0 * usage.free / usage.total if usage.total else -1.0
    except OSError:
        return -1.0


def _disk_space_ok(path: Path, config: Config) -> bool:
    """V3 disk-space guard: False when the destination volume is too full.

    ``disk_space_guard`` is the minimum percentage of free space that must
    remain on the destination volume; ``0`` disables the guard. A volume
    that cannot be queried (unusual filesystems, permission errors) is
    treated as OK — failing closed here would strand files forever.
    """
    threshold = float(getattr(config, "disk_space_guard", 0.0) or 0.0)
    if threshold <= 0:
        return True
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return True
    try:
        return (100.0 * usage.free / usage.total) >= threshold
    except ZeroDivisionError:
        return True


def _staged_move(path: Path, final: Path) -> None:
    """Crash-safe cross-volume move: copy -> fsync -> rename -> delete.

    ``shutil.move`` across drives is plain copy+delete: power loss mid-copy
    leaves a partial destination file alongside the intact source, and the
    journal would never know. Staging inside the destination directory
    (same volume as ``final``) makes the commit step a same-volume
    ``os.replace``:

    - crash during the copy -> only a ``<name>.part`` temp remains
      (ignored by every scan via the default ignore list), source untouched
    - ``os.replace`` is atomic -> the destination is absent or complete
    - the source is unlinked only after the rename committed

    Raises OSError on failure; the caller cleans up the temp file.
    """
    temp = final.with_name(final.name + ".part")
    try:
        with open(path, "rb") as src, open(temp, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        # V3 integrity verify: the whole point of staging is that the source
        # is only deleted after the destination is provably complete. A
        # size/hash mismatch means the copy is not trustworthy — abort
        # (the temp is removed below, the source survives untouched).
        if not _verify_copy(path, temp):
            raise OSError(f"integrity check failed copying {path} -> {final}")
        os.replace(str(temp), str(final))
        _fsync_dir(final.parent)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # The destination copy is committed; only the source delete
            # failed (e.g. the file got locked between copy and unlink).
            # Deleting an already-copied source must not undo a committed
            # move — log it and report success.
            logger.warning(
                "Copied %s -> %s but could not delete the source: %s", path, final, exc
            )
    except BaseException:
        # Never leave a partial staging file behind on a mid-copy failure.
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def move_file(path: Path, category: str, config: Config) -> MoveResult:
    """
    Move `path` into its category folder under the target root.

    Returns a MoveResult; never raises for routine problems.
    """
    try:
        if is_ignored(path, config.effective_ignore_patterns()):
            return MoveResult(skipped=True, reason="ignored")

        # V2 age policy: too-new (still being written) or too-old (user's
        # archives) files stay where they are. Reported as a plain skip so
        # callers treat it like any other "no action taken".
        if not age_policy_allows(path, config):
            logger.debug("Age policy: leaving %s in place", path.name)
            return MoveResult(skipped=True, reason="outside age policy")

        dest_dir = destination_for(path, category, config)
        resolved_dest_dir = dest_dir.resolve()
        target_root = config.resolved_target_root()
        # Category names come from config; refuse to let one escape the
        # target root (e.g. "../Outside").
        if not _is_inside(resolved_dest_dir, target_root):
            logger.error(
                "Refusing to move %s: category %r resolves outside the target root (%s)",
                path.name,
                category,
                resolved_dest_dir,
            )
            return MoveResult(
                skipped=True, refused=True,
                reason=f"category {category!r} escapes the target root",
            )

        dest = dest_dir / path.name
        if config.dry_run:
            logger.info("[dry-run] Would move %s -> %s", path, dest)
            return MoveResult(would_move=True, destination=dest)

        # V3 disk-space guard: a nearly-full destination volume turns a move
        # into a half-copied file (or a failed one); leave the source alone.
        if not _disk_space_ok(dest_dir, config):
            logger.warning(
                "Disk space guard: %.1f%% free on %s is below the configured "
                "minimum; leaving %s in place",
                _free_percent(dest_dir), dest_dir, path.name,
            )
            return MoveResult(skipped=True, reason="destination volume nearly full")

        dest_dir.mkdir(parents=True, exist_ok=True)
        # Check-then-move must be atomic against other workers: the lock
        # closes the race where both pass the existence check and the
        # second move silently overwrites the first.
        with _lock_for(resolved_dest_dir):
            final = _unique_destination(dest)
            size = path.stat().st_size  # before the move: source may be gone
            if _same_volume(path, final):
                shutil.move(str(path), str(final))
            else:
                # Cross-volume: stage the copy so a crash can never leave a
                # partial destination next to an intact source.
                _staged_move(path, final)
        # Pass the category that decided the move — deriving it from the
        # destination would record the YYYY-MM month folder as the category
        # when use_date_subfolders is on (flaw #40).
        journal.append_move(config, path, final, size, category=category)
        logger.info("Moved %s -> %s", path, final)
        return MoveResult(moved=True, destination=final)
    except FileNotFoundError:
        logger.warning("File vanished before move: %s", path)
        return MoveResult(skipped=True, reason="file vanished")
    except PermissionError as exc:
        if getattr(exc, "winerror", None) == 32:
            # ERROR_SHARING_VIOLATION: antivirus scan, cloud-sync upload or an
            # open player briefly holds the file. Transient by nature — report
            # it so the caller can retry later; the file stays where it is.
            logger.info("File temporarily locked (WinError 32): %s", path)
            return MoveResult(skipped=True, transient=True, reason="temporarily locked")
        logger.warning("Permission denied moving %s: %s", path, exc)
        return MoveResult(skipped=True, reason=f"permission denied: {exc}")
    except shutil.SameFileError:
        # Source and destination are the same file: already organized.
        logger.debug("Skipping %s (already at its destination)", path)
        return MoveResult(skipped=True, reason="already at destination")
    except OSError as exc:
        logger.warning("OS error moving %s: %s", path, exc)
        return MoveResult(skipped=True, reason=f"OS error: {exc}")
