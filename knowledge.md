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
watcher.Organizer ─────── decision logic: quiet-hours gate → ignore patterns →
      │                   target-root loop guard → category_for() (sub_rules →
      │                   name_rules → extension table) → ai fallback
      ▼
mover.move_file ───────── destination_for() + collision-safe rename +
      │                   per-directory locks; dry-run aware; WinError 32
      │                   reported as transient (retried via debouncer);
      │                   disk-space guard (free%% below threshold → leave
      │                   in place); cross-volume moves staged
      │                   (copy→fsync→SHA-256 verify→rename→delete)
      ▼
journal.py ────────────── append-only JSONL move journal (~/.fs-organizer/
      │                   moves.jsonl): audit trail + dashboard creation-date
      │                   source; best-effort, never breaks a move; exported
      │                   by export_journal() (CSV/JSON)
      ├── undo.py ──────── journal run backwards: restore N most recent moves,
      │                   never overwrite (collision suffix), never delete
      ├── suggest.py ───── rule suggestions from skipped files + journal stats
      └── ai_cache.py ──── persistent AI decision cache (provider+model+ext+
                           preview hash; atomic writes; successes only)
config.load_config ────── single validation entry point; ConfigError only;
                          FSORG_DRY_RUN / FSORG_TARGET_ROOT applied last
diagnostics.py ────────── check/watch-diag data + human renderings
duplicates.py ─────────── report-only dupes scan + opt-in quarantine move
                          (all-but-oldest aside, never deletes)
views.py ──────────────── dashboard payload builders (no HTTP)
ui.py ─────────────────── HTTP/session layer; serves dashboard.html;
                          token-protected POST /api/once|pause|reload|undo,
                          read-only GET /api/export (journal download)
```

Entry points:

- `python -m fs_organizer CONFIG [--once] [-v] [--log-file F] [--ui] [--port N] [--no-browser]` (`__main__.py`)
- Subcommands: `check`, `watch-diag`, `dupes`, `organize`, `undo`, `suggest`,
  `quarantine`, `export`, `init` (all take `CONFIG` first; `init` runs before
  config load because the file usually does not exist yet)
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
   live in `tests/test_flaw_regressions.py` (flaws #1–#50, one class each);
   the shared `make_config` builder lives in `tests/helpers.py`. Lint:
   `ruff check .` must stay clean (`[tool.ruff]` in `pyproject.toml`,
   enforced in CI before the test job).
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
   the Rules card's watched-folder list (#42). V2 additions live under the
   same rule: the age policy surfaces as its own `age`/`would_skip_age`
   decision, and plan rows report the template-rendered destination with
   the deciding category verbatim (#43).
14. V2 age policy (`age_policy`) and destination templates
    (`destination_template`) are enforced inside `move_file` and pre-checked
    in `_scan_files`/`_one_shot` — every consumer sees the same decision.
    The template must contain `{category}` and stays relative to the target
    root; the mover's traversal guard (#12) applies to rendered
    destinations exactly as to plain ones.
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
13. V2 runtime controls: the single-instance guard (runtime.py) means one
    daemon per resolved config path — a second start exits 3 unless
    `--force`. Stale locks (dead pid) are replaced, never require manual
    cleanup. Pause HOLDS events (bounded by the debouncer), never drops
    them; stop() releases held paths before draining. Live reload swaps
    the config ONLY through DashboardState.config — Dashboard.config is a
    read-only property over it (no second source of truth); a rejected
    reload keeps the old config active and the watcher's watches are fully
    reverted on a failed apply.
15. The paused flag and the `_held` set are mutated under ONE lock
    (`Watcher._pause_lock`): `pause()` sets it, `resume()` clears it inside
    the same critical section that swaps `_held`, and the debouncer dispatch
    callback reads it inside that lock too. Reading it outside the lock lets a
    dispatch pass the check, block on the lock while `resume()` swaps, then
    insert into the fresh `_held` set — a file no resume would ever dispatch
    (flaw #47).
16. `/api/status` (polled every 2s by the dashboard) must stay cheap: it may
    not embed `plan_summary()` or any other full-folder scan. The plan is
    served on demand at `/api/plan` (flaw #45).17. Every value interpolated into `dashboard.html` via `innerHTML` must be
escaped at the point of interpolation, including config-derived strings
(`target_root`, sub-rule patterns/extensions/categories). Pre-escaped
markup is the only exception, and it must be assembled explicitly (flaw
#49).
18. `sub_rules` patterns are `fnmatch` globs against the resolved path, and
    they expand `~` the same way `watch_folders`/`target_root` do — any
    path-shaped field a user writes must behave consistently, or a
    documented example silently matches nothing. The expansion MUST stay
    narrow: only a bare `~` or `~` + separator (`/` or `\`), because
    `os.path.expanduser("~scan.pdf")` yields `C:\Users\scan.pdf` on Windows
    (CPython reads everything after a lone `~` as a USERNAME) and would
    rewrite a legal file-NAME glob. So `~scan.pdf` matches the literal file
    name and `~someoneelse/x` stays unexpanded. Expansion happens at MATCH
    time (`SubRule.match_pattern()`), never by rewriting the stored
    `pattern` — otherwise `to_dict()`/`check --json` would bake an absolute
    home path into the user's config. `match_pattern()` also normalizes
    backslashes to forward slashes, since the matched form is
    `Path.as_posix()`.
19. V3 mutations are opt-in and reversible: `quarantine` moves duplicates
    aside but never deletes (the oldest copy of every group stays put),
    `undo` never overwrites a re-occupied original (collision suffix) and
    never rewrites the journal, and both honor `dry_run`. The AI cache
    stores only successful classifications (a transient outage must never
    poison it) and degrades to "no cache" when unreadable. The disk-space
    guard fails OPEN on an unqueryable volume (failing closed would strand
    files forever) and the staged-move verify aborts before the rename (a
    bad copy never commits; the source survives untouched).
20. New dashboard mutations (`POST /api/undo`) carry the same protections
    as the existing ones: loopback-only, `X-Auth-Token` required, bounded
    input (`count` is an int clamped to 0–500), and a 409 with a clear
    message when prerequisites (journal) are missing. `GET /api/export` is
    read-only like every other GET and therefore tokenless.