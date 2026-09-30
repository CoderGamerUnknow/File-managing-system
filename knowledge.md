# fs-organizer — Project Knowledge

## Goal

**A low-resource, event-driven background file manager for Windows 11.**

Watches user folders (Desktop, Documents, Downloads, …), and as files appear,
sorts them into category subfolders (`~/Organized/<Category>/`) using
extension rules — with an optional AI fallback for unknown extensions. It
must run forever at login (pythonw, no console), cost almost nothing when
idle, never lose or overwrite user data, and never organize its own output.

## Governing skills

Contributors (human or agent) must honor the rules in `.freebuff/skills/`:

| Skill | Governs |
| --- | --- |
| `.freebuff/skills/low-resource-daemon.md` | Memory/CPU budget, threading model, event hooks |
| `.freebuff/skills/windows-compatibility.md` | Win11 paths, temp-download ignores, file locks (WinError 32) |
| `.freebuff/skills/safe-file-operations.md` | No overwrites, collision suffixing, graceful error logging |

## Architecture (module map)

```
watchdog events
      │
      ▼
watcher._EventHandler ── schedules only paths inside watched roots
      │
      ▼
pool.Debouncer ────────── per-path quiet window (file_stable_seconds);
      │                   fires callback OUTSIDE its lock (re-entrant safe)
      ▼
pool.WorkerPool ───────── 2 daemon workers, bounded queue (drop-on-full),
      │                   submit_unique(): duplicate events for one file
      ▼                   coalesce into one task (+ one follow-up pass)
watcher.Organizer ─────── decision logic: ignore patterns → target-root
      │                   loop guard → rules.match_extension → ai fallback
      ▼
mover.move_file ───────── destination_for() + collision-safe rename +
      │                   per-directory locks; dry-run aware; WinError 32
      │                   reported as transient (retried via debouncer);
      │                   cross-volume moves staged (copy→fsync→rename→delete)
      ▼
journal.py ────────────── append-only JSONL move journal (~/.fs-organizer/
      │                   moves.jsonl): audit trail + dashboard creation-date
      ▼                   source; best-effort, never breaks a move
config.load_config ────── single validation entry point; ConfigError only
diagnostics.py ────────── check/watch-diag data + human renderings
views.py ──────────────── dashboard payload builders (no HTTP)
ui.py ─────────────────── HTTP/session layer; serves dashboard.html
```

Entry points:

- `python -m fs_organizer CONFIG [--once] [-v] [--log-file F] [--ui] [--port N] [--no-browser]` (`__main__.py`)
- `fs-organizer` console script (same)
- `scripts/setup_autostart.py` — one-time Windows login auto-start installer
- `scripts/smoke_dryrun.py` — end-to-end dry-run smoke test (real child process)
- `scripts/smoke_ui.py` — end-to-end web-dashboard smoke test (real child process)
- `fs_organizer/ui.py` — optional stdlib-only local web dashboard (loopback only; POST endpoints token-protected); `--ui` wires its ActivityLog into the Watcher/Organizer

## Non-negotiable invariants

1. Failures are safe by design: worst case is "nothing happened", never data loss.
2. Never touch anything inside the target root (loop guard) or outside the
   watched roots (scope guard).
3. Every long-lived thread is a daemon and is stopped by `Watcher.stop()`,
   which drains pending debounced files and queued work first.
4. All config validation lives in `config.load_config`; errors are
   `ConfigError` (exit code 2 from the CLI).
5. `classify_with_ai` never raises; unknown/disallowed AI answers leave the file in place.
6. Tests: `python -m pytest` must stay green; timing-sensitive regressions
   live in `tests/test_flaw_regressions.py` (flaws #1–#43, one class each);
   the shared `make_config` builder lives in `tests/helpers.py`.
7. The dashboard must never bind beyond loopback, and its HTML must escape
   user-influenced strings (file names) before rendering — see `ui.py`.
8. The ignore decision is always `Config.effective_ignore_patterns()` —
   built-in defaults + user list — in the mover, watcher, one-shot, and
   every diagnostic. No consumer may use the raw user list.
9. Every filesystem scan (`_scan_files`, `--once`, the watcher) covers the
   same scope: the top level of each watch folder, plus all subfolders when
   `recursive: true` (explicit opt-in). Plans, diagnostics, and dashboard
   payloads must never advertise a scope the watcher does not act on —
   including per-file details like watch-diag's `matched_rules` (#41) and
   the Rules card's watched-folder list (#42).
10. Config dumps are redacted: `to_dict()` never emits `ai.api_key` in
    cleartext (`check --json`, `watch-diag --json`).
11. The move journal is owned solely by `journal.py` (append/read, torn-line
    tolerance). Recording is best-effort: an unwritable journal must never
    break a move. When enabled, its `ts` is the authoritative creation date
    the dashboard shows for organized files (mtime lies on birthtime-less
    filesystems), and its `category` is the rule/AI category that decided
    the move — never the `YYYY-MM` date-subfolder (flaw #40). The same rule
    binds every payload builder: report the deciding category verbatim,
    never a path-derived lookalike (plan rows: flaw #43).
12. Dashboard events carry a monotonic, gap-free `seq`. Incremental polling
    (`/api/events?since=N`) must never miss or duplicate events across
    ring-buffer wraps; clients detect ring-fall-off or a counter reset via
    `oldest`/`latest` and re-sync from a full snapshot.
