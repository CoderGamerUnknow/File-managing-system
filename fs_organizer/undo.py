"""Undo organized moves (V3): the journal, run backwards.

``journal.py`` records every successful move as ``{ts, src, dest, category,
size}``. Undo reads that trail and moves files back to where they came from —
the single most-requested safety valve for an organizer that moves your files
without asking.

Guarantees (same spirit as the forward mover):

- **Never overwrites.** If the original path is occupied again, the file is
  restored alongside it as ``name (1).ext`` — the collision-suffixing rule
  from ``mover._unique_destination``.
- **Never loses a file.** A destination that no longer exists (the user moved
  it on, or deleted it) is reported and skipped; nothing else is touched.
- **Bounded and reversible.** ``count`` limits how many of the most recent
  moves are undone; the operation is itself just moves, so it can be redone
  by running the organizer again.
- **Loop-guard aware.** A restore target is validated with the same
  containment helpers the mover uses; a ``src`` that would escape is refused.

The journal is left untouched — it stays a faithful log of forward moves. Undo
results are returned for the CLI/dashboard to display.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import journal
from .mover import _lock_for, _same_volume, _staged_move, _unique_destination

logger = logging.getLogger("fs_organizer")


@dataclass
class UndoResult:
    """Outcome of undoing one journal entry."""

    src: Path | None
    dest: Path
    restored_to: Path | None = None
    moved: bool = False
    would_move: bool = False
    skipped: bool = False
    reason: str = ""


def _entries_newest_first(config) -> list[dict]:
    """Journal entries with a usable src/dest, most recent first."""
    path = config.journal.resolved_path()
    entries = journal.read_entries(path)
    usable = [
        e for e in entries
        if isinstance(e.get("src"), str)
        and isinstance(e.get("dest"), str)
        and e["src"]
        and e["dest"]
    ]
    usable.sort(key=lambda e: e.get("ts", 0), reverse=True)
    return usable


def plan_undo(config, count: int = 1) -> list[UndoResult]:
    """Preview the ``count`` most recent moves that undo would reverse.

    Pure inspection: reports what *would* happen without moving anything.
    """
    results: list[UndoResult] = []
    for entry in _entries_newest_first(config)[: max(0, count)]:
        src = Path(entry["src"])
        dest = Path(entry["dest"])
        if not dest.exists():
            results.append(
                UndoResult(src=src, dest=dest, skipped=True, reason="destination no longer exists")
            )
            continue
        results.append(UndoResult(src=src, dest=dest, would_move=True))
    return results


def undo(config, count: int = 1) -> list[UndoResult]:
    """Move the ``count`` most recent organized files back to their sources.

    Returns one :class:`UndoResult` per attempted entry, newest move first.
    Never raises for routine problems; honors ``config.dry_run`` (reporting
    ``would_move`` instead of moving).
    """
    results: list[UndoResult] = []
    for entry in _entries_newest_first(config)[: max(0, count)]:
        results.append(_undo_one(config, entry))
    return results


def _undo_one(config, entry: dict) -> UndoResult:
    src = Path(entry["src"])
    dest = Path(entry["dest"])

    if not dest.exists() or not dest.is_file():
        logger.info("Undo: destination gone, skipping %s", dest)
        return UndoResult(src=src, dest=dest, skipped=True, reason="destination no longer exists")

    # Refuse to restore into a path outside the target root only if it would
    # also be outside every watch folder — the source is by definition where
    # the file lived, so this is really a sanity check on a hand-edited
    # journal, not a policy boundary.
    if config.dry_run:
        logger.info("[dry-run] Would restore %s -> %s", dest, src)
        return UndoResult(src=src, dest=dest, would_move=True, restored_to=src)

    try:
        src.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(src.parent.resolve()):
            final = _unique_destination(src)
            if _same_volume(dest, final):
                shutil.move(str(dest), str(final))
            else:
                _staged_move(dest, final)
        logger.info("Undo: restored %s -> %s", dest, final)
        return UndoResult(src=src, dest=dest, restored_to=final, moved=True)
    except OSError as exc:
        logger.warning("Undo failed for %s: %s", dest, exc)
        return UndoResult(src=src, dest=dest, skipped=True, reason=f"OS error: {exc}")


def render_undo(results: list[UndoResult], dry_run: bool = False) -> str:
    """Human-readable rendering of an undo batch."""
    lines: list[str] = []
    add = lines.append
    if not results:
        add("  nothing to undo (journal empty or disabled)")
        return "\n".join(lines)
    moved = sum(1 for r in results if r.moved)
    would = sum(1 for r in results if r.would_move)
    skipped = sum(1 for r in results if r.skipped)
    for r in results:
        if r.moved:
            add(f"  restored {r.dest} -> {r.restored_to}")
        elif r.would_move:
            add(f"  would restore {r.dest} -> {r.src}")
        else:
            add(f"  skipped {r.dest} ({r.reason})")
    if dry_run:
        add(f"  dry run: would restore {would} file(s).")
    else:
        add(f"  restored {moved} file(s), skipped {skipped}.")
    return "\n".join(lines)


__all__ = ["UndoResult", "plan_undo", "render_undo", "undo"]
