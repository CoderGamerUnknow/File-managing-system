# Skill: Safe File Operations

User data is never lost, overwritten, or left half-moved. When in doubt, do
nothing and log.

## Never overwrite

1. Before any move, `mover._unique_destination` guarantees a free target:
   on collision the file becomes `name (1).ext`, `name (2).ext`, … capped at
   999, after which it raises `FileExistsError` (logged, file left in place).
2. The existence check + move are serialized by a **per-destination-directory
   lock** (`mover._dir_locks`). Without it, two workers moving same-named
   files from different watch folders both pass the check and the second
   `shutil.move` silently overwrites the first (verified data loss; guarded
   by `tests/test_flaw_regressions.py` flaw #1).

## Refuse dangerous destinations

3. The category segment comes from config/AI and is untrusted: the resolved
   destination dir must satisfy `is_relative_to(target_root)` or the move is
   refused and logged (blocks `../` traversal, e.g. category `../../Outside`).
4. Files already inside the target root are never re-processed (loop guard,
   both in `Organizer.handle` and `__main__._one_shot`).
5. Events outside the watched roots are never scheduled (scope guard in
   `_EventHandler`), so the organizer never yanks files from elsewhere.

## Contained failure modes

6. `move_file` returns a `MoveResult` and never raises for routine problems:
   vanished files (`FileNotFoundError`), access denied, `SameFileError`
   (already at destination), and other `OSError`s all become
   `skipped=True` + a log line. `transient=True` (WinError 32 lock) is
   retried via the debouncer — see windows-compatibility skill.
7. Dry-run (`dry_run: true`) logs `[dry-run] Would move X -> Y`, moves
   nothing, creates no directories, and counts `would_move` for the
   `--once` summary.
8. Worker tasks must never die: `WorkerPool` and `Debouncer` catch and log
   callback exceptions and keep serving. `classify_with_ai` never raises;
   disallowed/unknown AI categories leave the file untouched.

## Logging

9. Every decision worth auditing is logged at INFO (`Moved X -> Y`,
   `[dry-run] Would move …`); every refusal/failure at WARNING/ERROR with
   the reason and path. Handlers: console when stderr exists + rotating file
   handler (1 MB × 3) — see low-resource-daemon skill for caps.
10. `--once` prints an accurate summary: `Organized N file(s)` or
    `Dry run: would organize N file(s)` — counts must reflect reality
    (flaw #8 regression guards the dry-run count).
