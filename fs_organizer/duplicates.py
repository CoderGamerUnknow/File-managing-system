"""Duplicate detection (V2, report-only).

Finds duplicate files under the watch folders by size pre-filter + full
content hash (SHA-256). Deliberately **report-only**: it never moves,
deletes, or rewrites anything — the roadmap's first stage for duplicates is
visibility, and a report that can destroy data would break the
safe-file-operations invariant (worst case is "nothing happened").

Resource bounds (low-resource-daemon skill):

- Size pre-filter: files whose byte size is unique among the candidates are
  provably unique — their content is never read. Hashing only starts once
  two files share a size.
- Streaming hashes: files are read in 1 MiB chunks; a multi-GB duplicate
  costs constant memory.
- ``max_bytes_hashed`` caps the total content read per scan so pointing the
  organizer at a huge tree cannot turn a report into a disk-thrashing job.
  Groups discovered before the cap are complete; the cap can only stop NEW
  groups from being verified (reported via ``truncated``).
- ``max_files`` bounds the walk itself (same convention as plan()).

Pure functions over ``Config``; no journal, no activity feed, no HTTP.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from .mover import _is_inside, _iter_files
from .rules import is_ignored

_HASH_CHUNK = 1024 * 1024  # 1 MiB streaming chunks


def hash_file(path: Path, chunk_size: int = _HASH_CHUNK) -> str:
    """SHA-256 of *path*'s content, streamed (constant memory).

    Raises OSError on unreadable files — callers decide the policy; a
    vanished file must not silently masquerade as "empty" (a hash of
    nothing would collide with other unreadable files).
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicates(
    config,
    max_files: int = 2000,
    max_bytes_hashed: int = 256 * 1024 * 1024,
    min_size: int = 1,
) -> dict:
    """Report duplicate files under the watch folders (report-only).

    Walks exactly the scope the watcher acts on (top level, plus subfolders
    when ``config.recursive``), applies the same effective ignore list and
    target-root loop guard as the organizer, then:

    1. groups candidates by size (unique sizes are skipped unread),
    2. streams a SHA-256 per still-ambiguous file,
    3. reports groups of 2+ identical hashes.

    Each group is ``{hash, size, files: [{path, size, mtime}]}`` sorted
    oldest-mtime first — the natural "keep the original, these are copies"
    reading. ``bytes_hashed`` reports the content actually read; the scan
    stops hashing new groups once ``max_bytes_hashed`` is exceeded and
    reports ``truncated: true``.

    Never mutates anything and never raises for routine problems: a file
    that vanishes or cannot be read mid-scan is skipped (it cannot be
    proven a duplicate, so it is not reported as one).
    """
    ignore_list = config.effective_ignore_patterns()
    target_root = config.resolved_target_root()

    # --- pass 1: walk + size grouping (no content read) -------------------
    by_size: dict[int, list[Path]] = {}
    walked = 0  # total files yielded by the walk (drives the max_files cap)
    scanned = 0  # files that made it past every filter (candidates only)
    truncated_walk = False
    # Use expanded (not resolved) folders so a missing watch folder is still
    # reported in the output rather than silently dropped.
    folders = config.expanded_watch_folders()
    for folder in folders:
        if not folder.is_dir():
            continue  # missing watch folder: tolerate, don't crash
        for path in _iter_files(folder, config.recursive):
            if walked >= max_files:
                truncated_walk = True
                break
            walked += 1
            if _is_inside(path, target_root):
                continue  # organized output is never a duplicate candidate
            if is_ignored(path, ignore_list):
                continue
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue  # vanished / unreadable: not provably a duplicate
            if size < min_size:
                continue
            scanned += 1
            by_size.setdefault(size, []).append(path)
        if truncated_walk:
            break

    # --- pass 2: hash only the size-ambiguous files ------------------------
    groups: list[dict] = []
    bytes_hashed = 0
    files_hashed = 0
    hashing_truncated = False

    for size in sorted(by_size, reverse=True):  # big wins first
        candidates = by_size[size]
        if len(candidates) < 2:
            continue  # unique size -> provably unique content; never read
        hashes: dict[str, list[Path]] = {}
        for path in candidates:
            # Check the budget per-file so a size group that alone exceeds
            # the cap still truncates instead of reading unbounded content.
            if bytes_hashed >= max_bytes_hashed:
                hashing_truncated = True
                break
            try:
                digest = hash_file(path)
            except OSError:
                continue  # unreadable: skip, never report it
            bytes_hashed += size
            files_hashed += 1
            hashes.setdefault(digest, []).append(path)
        for digest, paths in hashes.items():
            if len(paths) < 2:
                continue
            rows = []
            for p in paths:
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0.0
                rows.append({"path": p, "size": size, "mtime": mtime})
            rows.sort(key=lambda r: r["mtime"])  # oldest first = "the original"
            groups.append({"hash": digest, "size": size, "files": rows})

    groups.sort(key=lambda g: (-g["size"], g["hash"]))
    wasted = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    return {
        "watch_folders": [str(w) for w in config.expanded_watch_folders()],
        "scanned_files": scanned,
        "files_hashed": files_hashed,
        "bytes_hashed": bytes_hashed,
        "max_bytes_hashed": max_bytes_hashed,
        "duplicate_groups": groups,
        "duplicate_files": sum(len(g["files"]) for g in groups),
        "wasted_bytes": wasted,
        "truncated": truncated_walk or hashing_truncated,
    }


def quarantine_duplicates(
    config,
    report: dict,
    quarantine_root: Path,
    dry_run: bool | None = None,
) -> dict:
    """Move duplicate copies into a quarantine folder (V3, opt-in).

    The actionable counterpart to the report-only scan, and still **nothing
    is ever deleted**: for each group, the oldest file stays where it is (the
    presumed original) and every other copy is moved into
    ``<quarantine_root>/<date>/<hash>/`` with the collision-suffixing rule,
    so the operation is fully reversible by hand or with ``undo``.

    ``dry_run`` defaults to ``config.dry_run``. Returns a summary dict with
    the moved files and any that were skipped (locked/vanished).
    """
    import shutil

    from .mover import _lock_for, _same_volume, _staged_move, _unique_destination

    if dry_run is None:
        dry_run = bool(getattr(config, "dry_run", False))
    moved: list[dict] = []
    skipped: list[dict] = []
    stamp = datetime.now().strftime("%Y-%m-%d")

    for group in report.get("duplicate_groups", []):
        files = group.get("files", [])
        if len(files) < 2:
            continue
        # Oldest first is already the report order; keep index 0 as original.
        for row in files[1:]:
            src = Path(row["path"])
            if not src.is_file():
                skipped.append({"path": str(src), "reason": "no longer exists"})
                continue
            dest_dir = Path(quarantine_root) / stamp / group["hash"][:12]
            if dry_run:
                moved.append({"path": str(src), "dest": str(dest_dir / src.name)})
                continue
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                with _lock_for(dest_dir.resolve()):
                    final = _unique_destination(dest_dir / src.name)
                    if _same_volume(src, final):
                        shutil.move(str(src), str(final))
                    else:
                        _staged_move(src, final)
                moved.append({"path": str(src), "dest": str(final)})
            except OSError as exc:
                skipped.append({"path": str(src), "reason": str(exc)})

    return {
        "dry_run": dry_run,
        "quarantine_root": str(quarantine_root),
        "moved": moved,
        "skipped": skipped,
        "moved_count": len(moved),
    }


def render_quarantine(data: dict) -> str:
    """Human-readable rendering of :func:`quarantine_duplicates` output."""
    lines: list[str] = []
    add = lines.append
    verb = "would quarantine" if data.get("dry_run") else "quarantined"
    add(
        f"  {verb} {data['moved_count']} duplicate copy(ies) into "
        f"{data['quarantine_root']} (originals stay in place; nothing deleted)"
    )
    for row in data.get("moved", []):
        add(f"    {row['path']} -> {row['dest']}")
    for row in data.get("skipped", []):
        add(f"    skipped {row['path']} ({row['reason']})")
    return "\n".join(lines)


def render_duplicates(data: dict) -> str:
    """Human-readable rendering of :func:`find_duplicates` output."""
    lines: list[str] = []
    add = lines.append
    groups = data["duplicate_groups"]
    add(
        f"  scanned {data['scanned_files']} file(s), hashed {data['files_hashed']} "
        f"({data['bytes_hashed']} bytes) — unique sizes were never read"
    )
    if data["truncated"]:
        add("  TRUNCATED: file cap or hashing budget reached; more duplicates may exist")
    if not groups:
        add("  no duplicates found")
        return "\n".join(lines)
    add(
        f"  {len(groups)} duplicate group(s), {data['duplicate_files']} file(s), "
        f"{data['wasted_bytes']} reclaimable bytes (report only — nothing was moved)"
    )
    for group in groups:
        add(
            f"  [{group['size']} bytes, sha256 {group['hash'][:12]}…] "
            f"{len(group['files'])} identical file(s):"
        )
        for row in group["files"]:
            add(f"    {row['path']}  (mtime {row['mtime']:.0f})")
    return "\n".join(lines)
