# Skill: Windows 11 Compatibility

fs-organizer targets Windows 11 first (see `scripts/setup_autostart.py`),
but must not *break* on POSIX. Follow these rules in every code path that
touches paths, files, processes, or signals.

## Paths

1. Always build paths with `pathlib.Path` and expand users with
   `os.path.expanduser` (config does this in `resolved_watch_folders()` /
   `resolved_target_root()`). Never concatenate path strings with `\\`.
2. Compare paths only after `.resolve()` (case/alias safety on Win11, e.g.
   OneDrive-redirected folders). The watcher's dedupe key and the mover's
   traversal guard both use resolved paths.
3. Known folders may be redirected (OneDrive: Desktop/Documents/Pictures).
   `setup_autostart.py` resolves the real locations from
   `HKCU\...\Explorer\User Shell Folders` — keep using that, never assume
   `%USERPROFILE%\<Name>` exists.
4. Never watch or modify `AppData`, `Windows`, `Program Files` — the
   autostart script excludes them (`ALWAYS_EXCLUDE`), and no default rule
   may ever produce a category path outside the target root.

## Temporary / in-progress downloads (must be ignored)

Files still being written by browsers and sync clients must never be moved.
The default ignore set (config.py `DEFAULT_IGNORE_PATTERNS`, also in
`config.example.json`) covers: `.*`, `*.tmp`, `*.part`, `*.crdownload`,
`*~`, `desktop.ini`, `Thumbs.db`; the autostart config adds Office lock
files `~$*`, `~*.docx`, `~*.xlsx`, `~*.pptx`. Debounce
(`file_stable_seconds`) is the second line of defense — keep both.
When matching globs: a pattern without `/` matches the file *name* only;
with `/` it matches the full (forward-slashed) path. See `rules.is_ignored`.

## File locks (WinError 32 = ERROR_SHARING_VIOLATION)

Antivirus scans, cloud-sync uploads, and open media players briefly hold
files. `shutil.move` then raises `PermissionError` with `winerror == 32`.

1. This error is **transient**: `mover.move_file` classifies it as
   `MoveResult(transient=True)` (distinct from plain access-denied, which is
   `skipped=True`).
2. The `Organizer` retries transiently-locked files by re-scheduling the
   path into the debouncer — the stability window doubles as backoff — up to
   `Organizer.MAX_LOCK_RETRIES` (3) attempts, then logs and gives up.
3. Never crash, never delete, never rename-aside the locked file. Worst case
   is "file stays where it is".
4. Any other `PermissionError` / `OSError` from a move is logged and the
   file is left in place (`MoveResult.skipped=True`). `move_file` never
   raises for routine problems.

## Processes, consoles, and signals

1. Auto-start uses `pythonw.exe` via `HKCU\...\Run` (no admin, no console).
   Under pythonw `sys.stdout`/`sys.stderr` are effectively unavailable — the
   CLI uses `_print()` (never raises) and only adds a `StreamHandler` when
   stderr exists; otherwise file logging or `logging.disable(CRITICAL)`.
2. Windows console signals (Ctrl+C / `CTRL_BREAK_EVENT`) cannot interrupt an
   in-progress `Event.wait()`. Main-loop waits must use short slices
   (`_wait_forever`, 0.5 s) or shutdown can hang for the whole timeout.
3. There is no `signal.pause()` on Windows; the CLI's stop event + try/
   KeyboardInterrupt pattern is the approved structure.

## Testing on Windows

`scripts/smoke_dryrun.py` runs the real CLI as a child process group and
sends `CTRL_BREAK_EVENT`; it verifies signal-interruptible shutdown and
that files pending at shutdown are drained. Keep it green.
