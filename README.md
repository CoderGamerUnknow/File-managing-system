<div align="center">

# fs-organizer

**A lightweight, low-resource, event-driven file organizer. It watches your folders and sorts files into category subfolders as they arrive — safely, and without ever losing data.**

[![CI](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml/badge.svg)](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-3b82f6?style=flat-square)](LICENSE)

</div>

## Overview

Built for Windows 11 but works anywhere Python runs. It sits at login, costs almost nothing
while idle, and whenever a file appears in a watched folder — Desktop, Documents, Downloads,
… — it files it into `~/Organized/<Category>/` using extension rules, with an optional AI
fallback for unknown types.

The design constraints are strict, and the whole codebase is shaped by them: **never lose or
overwrite user data, never organize its own output, and never stall the watcher.** Every
failure mode is designed to end in "nothing happened" rather than a half-moved file.

## Features

**Core**

- **Rule-based sorting** — map extensions to categories (`.pdf` → `Documents`, `.jpg` → `Images`, …).
- **Live watching** — via [watchdog](https://github.com/gorakhargosh/watchdog); created and moved files are picked up automatically.
- **Debounced events** — editors that save in bursts and partial downloads don't trigger half-written moves; a file is handled only once it has been quiet for a configurable grace period.
- **Non-blocking worker pool** — moves run on background threads so the watcher never stalls; duplicate events for one file coalesce into a single task.
- **Collision-safe** — a file that would overwrite an existing one becomes `name (1).ext`, `name (2).ext`, … with per-directory locking, safe even with several watch folders.
- **Ignore patterns** — globs for file names (no slash, `*.tmp`) or full paths (with a slash, `**/Downloads/**`).
- **Dry-run and one-shot modes** — preview everything, or organize what already exists with `--once` and exit.

**V2 — runtime controls and layout**

- Single-instance guard, pause/resume, live config reload.
- **Age policy** — files too new (still being written) or too old (archives) stay put.
- **Destination templates** — `{category}` and `{date:FORMAT}` tokens control the organized layout.
- **Per-category sub-rules** — route by source folder: a `.pdf` under `~/Downloads/invoices/` → `Invoices` while every other `.pdf` → `Documents`. Extension-gated, first match wins, evaluated before the global table.
- **Duplicate detection** — report-only SHA-256 scan with a size pre-filter, streamed hashing and a bounded walk.

**V3 — reversibility and intelligence**

- **Undo** — `fs-organizer CONFIG undo [N]` and the dashboard's **Undo last** restore recent moves from the journal, never overwriting a re-occupied original.
- **Quarantine cleanup** — moves every duplicate copy except the oldest into `_Duplicates/` for review. Nothing is ever deleted, and `undo` reverses it.
- **Name rules** — route by glob or regex (`invoice*` → `Invoices`, `^IMG_\d+` → `Photos`), after sub-rules and before the extension table.
- **Quiet hours** — organize only inside a daily `HH:MM` window, midnight wrap included.
- **AI decision cache** — opt-in, so repeat unknown files never re-hit the model.
- **Rule suggestions** — `suggest` mines the watch folders and journal for uncovered files and prints paste-ready config snippets.
- **Journal export** — `export --format csv|json`, also available from the dashboard.
- **Safety hardening** — a disk-space guard refuses moves onto a nearly-full volume, and cross-volume copies are SHA-256 verified before the source is deleted.
- **Stdlib-only AI client** — plain `urllib`, no SDK dependency.

## Requirements

Python 3.9+ and `watchdog` (the only runtime dependency).

```bash
pip install .            # installs the fs-organizer command + watchdog
```

Or install the dependency and run from the repo:

```bash
pip install watchdog     # runtime dependency
pip install pytest       # optional, for the tests
```

## Quick start

1. Copy `config.example.json` to `config.json` and edit it:

```json
{
  "watch_folders": ["~/Downloads"],
  "target_root": "~/Organized",
  "target_rules": {
    ".pdf": "Documents",
    ".jpg": "Images",
    ".zip": "Archives"
  },
  "ignore_patterns": [".*", "*.tmp", "*.part", "*.crdownload"],
  "use_date_subfolders": false,
  "dry_run": false,
  "file_stable_seconds": 1.0,
  "recursive": false,
  "journal": { "enabled": false, "path": null },
  "ai": { "enabled": false }
}
```

2. Try it without consequences (set `"dry_run": true` first to preview):

```bash
python -m fs_organizer config.json --once --verbose
```

In dry-run the one-shot scan prints `Dry run: would organize N file(s).` and touches nothing.

3. Run the watcher:

```bash
python -m fs_organizer config.json
```

## CLI

```
fs-organizer CONFIG [--once] [-v/--verbose] [--log-file F] [--ui] [--port N] [--no-browser] [--force] [--watch-files PATH]
fs-organizer CONFIG check [--json] [--watch-files PATH]
fs-organizer CONFIG watch-diag [--json] [--watch-files PATH]
fs-organizer CONFIG dupes [--json] [--max-files N] [--max-mb N] [--min-size BYTES]
fs-organizer CONFIG organize PATH... [--recursive] [--dry-run]
fs-organizer CONFIG undo [N] [--dry-run] [--json]
fs-organizer CONFIG suggest [--json] [--max-files N]
fs-organizer CONFIG quarantine [--dest PATH] [--dry-run] [--json]
fs-organizer CONFIG export [--format csv|json]
fs-organizer CONFIG init [--output PATH] [--force]
```

| Flag | Effect |
| --- | --- |
| `--once` | Organize existing files and exit (honours `dry_run`) |
| `-v` / `--verbose` | Debug logging |
| `--log-file F` | Also write logs to this file |
| `--ui` | Serve the web dashboard while watching |
| `--port N` | Dashboard port (default `8765`; `0` picks a free one) |
| `--no-browser` | With `--ui`: don't open a browser |
| `--force` | Start even if another instance holds this config's lock (otherwise exit code 3) |
| `--watch-files PATH` | With `check` / `watch-diag`: the ignore verdict for one path, without walking it |

### `check` — config and plan preview

Prints the resolved config (redacted), the effective ignore list, and a bounded plan for
every candidate file grouped by decision: `would_organize`, `would_classify_ai`,
`would_skip_pattern`, `would_skip_age`, `would_skip_unknown`. `--json` for a
machine-readable payload.

### `watch-diag` — why isn't my file moving?

Per watch folder: how many files are ignored (and how many of those would otherwise match a
rule), how many have no rule and are not AI-able, and the exact effective ignore patterns.

### `dupes` — find duplicate files (report-only)

Scans exactly the scope the watcher acts on and reports groups of 2+ identical files by
SHA-256. Nothing is moved, renamed or deleted. Resource bounds keep it safe on huge trees:
files with a unique byte size are never read, hashing streams in 1 MiB chunks, `--max-files`
caps the walk (default 2000), `--max-mb` caps total content (default 256 MiB) and reports
`truncated: true`, and `--min-size` skips tiny files (default 1).

### `undo` — move files back

Restores the `N` most recent organized files (default 1) to their original paths from the
journal. Requires `journal.enabled: true`. A re-occupied original is restored alongside as
`name (1).ext`; a vanished destination is skipped with a reason.

### `suggest` — which rule should I add next?

Scans the watch folders for files that match no rule and are not AI-able, prints ready-to-paste
`{ext: category}` suggestions ordered by file count, summarizes journal activity per
category and flags AI-allowed categories that nothing has used. Read-only.

### `quarantine` — move duplicate copies aside

Runs the duplicate scan, then moves every copy except the oldest of each group into
`<target_root>/_Duplicates/<date>/<hash>/`. Nothing is deleted.

### `export` — dump the journal

CSV (default) or JSON, one row per move (`ts,ts_iso,src,dest,category,size`). The dashboard
exposes the same dump at `GET /api/export?format=csv`.

### `init` — write a starter config

Creates a config that watches the user folders that exist in your home directory, with
`dry_run: true` already set. Refuses to overwrite without `--force`.

### Environment variables

| Variable | Effect |
| --- | --- |
| `FSORG_DRY_RUN` | `1`/`true`/`yes`/`on` forces preview mode for this process (overrides the config file; `0`/`false` forces real moves) |
| `FSORG_TARGET_ROOT` | Redirects `target_root` for this process only |
| `OPENAI_API_KEY` | Key for the `openai` AI provider |

## Configuration

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `watch_folders` | list of paths | *(required)* | Folders to watch; must exist at startup |
| `target_rules` | object | built-in set | `{ ".ext": "Category" }`; keys are case-insensitive |
| `ignore_patterns` | list of globs | *(none)* | Extra globs on top of the built-in set. A glob without a slash matches the file name; with a slash it matches the full path. The organizer always applies the built-in defaults **plus** your patterns — the same combined list `check` prints |
| `target_root` | path | `~/Organized` | Root for the category subfolders |
| `use_date_subfolders` | bool | `false` | Also group by mtime: `Documents/2026-10/`. Legacy; superseded by `destination_template` |
| `destination_template` | object | `{category}` | `{"pattern": "{category}/{date:%Y}/{date:%Y-%m}"}` → `Organized/Documents/2026/2026-10/a.txt`. Tokens: `{category}` (required) and `{date:FORMAT}` (strftime on mtime, default `%Y-%m`). Absolute patterns and patterns without `{category}` are rejected; a malformed date format falls back to `%Y-%m` instead of crashing a move |
| `age_policy` | object | *(disabled)* | `{"min_age_seconds": 0, "max_age_days": null}`. Younger files (still being written) and older files (archives) stay put. Skips are reported in `would_skip_age`, never as a planned move |
| `sub_rules` | list of objects | *(none)* | `{"pattern": "~/Downloads/invoices/**", "extensions": [".pdf"], "category": "Invoices"}`. Extension-gated, ordered, **first match wins**, takes priority over `target_rules`. `~` expands only as a bare `~` or `~/…`, so `~scan.pdf` still means the file name; backslashes are normalized |
| `name_rules` | list of objects | *(none)* | `{"pattern": "invoice*", "category": "Invoices"}` (glob) or `{"pattern": "^IMG_\\d+", "category": "Photos", "regex": true}`. Optional `extensions` narrows the rule. Evaluated after `sub_rules`, before `target_rules`. Invalid entries exit with code 2 |
| `quiet_hours` | object | *(disabled)* | `{"start": "22:00", "end": "06:00"}` — organize only inside this window (`HH:MM`; end ≤ start wraps midnight). Outside it files are left in place and picked up after the window opens |
| `disk_space_guard` | number | `0.0` | Minimum free-space percentage required on the destination volume (`0` = off). An unqueryable volume is treated as OK, so files are never stranded |
| `dry_run` | bool | `false` | Log intended moves without moving anything |
| `file_stable_seconds` | number | `1.0` | How long a file must be quiet before it moves |
| `recursive` | bool | `false` | Also watch every subfolder. Off by default; when on, the watcher, `--once`, plans and diagnostics all cover the deeper scope, and the target-root subtree stays excluded |
| `journal` | object | `enabled: false` | Append-only JSONL audit trail (`ts`, `src`, `dest`, `category`, `size`). Its `ts` is also the creation date the dashboard shows, since filesystems without birthtime would otherwise mislabel moved files with the move time. A torn final line is tolerated; recording is best-effort and never breaks a move |
| `journal.path` | path | `~/.fs-organizer/moves.jsonl` | Where the journal is written |
| `ai` | object | see below | Optional AI fallback |

### AI fallback

```json
"ai": {
  "enabled": true,
  "provider": "ollama",
  "model": "llama3.2",
  "base_url": "http://localhost:11434",
  "timeout_seconds": 15.0,
  "max_bytes_to_read": 65536,
  "extensions": [".xyz", ".dat"],
  "allowed_subfolders": ["Documents", "Images", "Other"],
  "cache_enabled": false,
  "cache_path": null
}
```

- **provider** — `ollama` (local, default) or `openai`; `api_key` is required for OpenAI and
  can come from `OPENAI_API_KEY`.
- **extensions** — only these are sent to the model.
- **allowed_subfolders** — the only categories the model may choose (required when enabled).
  Anything else is ignored and the file stays put. The model sees a truncated, lossily
  decoded preview.
- **cache_enabled** / **cache_path** — memoizes successful classifications at
  `~/.fs-organizer/ai_cache.json` (default), keyed by provider + model + extension + preview
  bytes. Failed requests are never cached, so an outage can't poison it.

Failures are safe by design: network errors, malformed replies and disallowed categories
leave the file untouched. The API key is redacted in every config dump.

## Web dashboard

```bash
python -m fs_organizer config.json --ui   # http://127.0.0.1:8765
```

- **Status** — target root, stability window, dry-run badge, AI summary.
- **Watched folders** and the **rule table**.
- **Recent activity** — every moved / skipped / dry-run decision, live. Events carry a
  gap-free monotonic `seq`, and the UI detects a ring-buffer wrap or counter reset and
  re-syncs from a full snapshot, so polling never misses or duplicates events.
- **Duplicates** — report-only SHA-256 groups with the oldest copy marked.
- **Files by date** — organized files grouped by creation date with month/year navigation,
  per-day summaries (count, total size, per-category breakdown), human-readable sizes and
  sorting. Uses Windows creation time where available, mtime otherwise.
- **Controls** — **Organize now**, **Pause/Resume** (holds events, never drops them),
  **Reload config** (a rejected reload keeps the old config and reverts the watches),
  **Undo last**, **Export**, and the single-instance indicator.
- **File filter** — debounced client-side search by name or category.

It binds to `127.0.0.1` only, and its mutating endpoints (`POST /api/once`, `/api/pause`,
`/api/reload`, `/api/undo`) require a per-process random token injected into the served
page. Read-only `GET`s expose only paths and metadata under your configured folders. No
dependencies beyond the standard library; an end-to-end check lives at `scripts/smoke_ui.py`.

`/api/status` is polled every 2 seconds and stays deliberately cheap (config, counters,
pause state). The plan preview walks every watch folder, so it is served on demand at
`GET /api/plan` instead.

## How it works

```
watchdog events
      │
      ▼
watcher._EventHandler ── schedules only paths inside watched roots
      │
      ▼
pool.Debouncer ───────── per-path quiet window (file_stable_seconds);
      │                  fires the callback OUTSIDE its lock (re-entrant safe)
      ▼
pool.WorkerPool ─────── 2 daemon workers, bounded queue (drop-on-full),
      │                  submit_unique(): duplicate events coalesce into one
      ▼                  task (+ one follow-up pass)
watcher.Organizer ───── decision logic: ignore patterns → target-root loop
      │                  guard → age policy → rules → AI fallback
      ▼
mover.move_file ─────── destination_for() + collision-safe rename +
      │                  per-directory locks; dry-run aware; WinError 32
      │                  treated as transient; cross-volume moves staged
      ▼                  (copy → fsync → rename → delete)
journal.py ──────────── append-only JSONL journal (~/.fs-organizer/moves.jsonl)
```

1. watchdog raises events for created and moved files.
2. The debouncer waits until the path has been quiet for `file_stable_seconds`.
3. The worker pool runs the organizer on a background thread, coalescing duplicate events
   so a file is never moved twice.
4. The organizer applies ignores, skips anything inside the target root, checks the age
   policy, resolves a category from the rules (or the AI fallback), and moves the file —
   creating the destination from `destination_template` and suffixing `(1)`, `(2)`, … on
   collisions.
5. With the journal enabled, the move is appended to the audit trail.

### Safety invariants

1. Failures are safe by design: the worst case is "nothing happened", never data loss.
2. Never touch anything inside the target root (loop guard) or outside the watched roots
   (scope guard).
3. Every long-lived thread is a daemon and is stopped by `Watcher.stop()`, which drains
   pending debounced files and queued work first.
4. All config validation lives in `config.load_config`; errors raise `ConfigError` (exit
   code 2 from the CLI).
5. `classify_with_ai` never raises; unknown or disallowed answers leave the file in place.
6. One lock per *destination directory* serializes the existence check plus the move, so two
   workers can never overwrite each other.
7. The dashboard never binds beyond loopback, and escapes user-influenced strings (file
   names) before rendering.
8. The ignore decision is always `Config.effective_ignore_patterns()` — built-in defaults
   plus the user list — everywhere. No consumer uses the raw user list.

## Start automatically at login (Windows)

```bash
python scripts/setup_autostart.py
```

This creates a live config at `~\.fs-organizer\config.json` watching your real user folders
(including OneDrive-redirected locations, resolved from the registry), registers an
`HKCU\...\Run` entry so `pythonw.exe -m fs_organizer <config>` starts hidden at every login
(no admin rights, no console window), and writes rotating logs to
`~\.fs-organizer\fs-organizer.log` (1 MB per file, 2 backups).

Remove it later with `python scripts/setup_autostart.py --remove`. The run key only starts
the program at login — to apply config changes immediately, restart the process or use the
dashboard's **Reload config**.

## Tests

```bash
python -m pytest
python scripts/smoke_dryrun.py   # e2e: dry-run + graceful shutdown
python scripts/smoke_ui.py       # e2e: dashboard reflects live activity
```

CI runs the suite plus both smoke tests on Ubuntu and Windows (Python 3.13, plus 3.9 on one
runner to guard the floor). On failure it publishes the pytest and smoke logs to a
`ci-logs-<os>-py<ver>` branch and as workflow artifacts, so a red run is diagnosable without
re-running anything.

## Project layout

```
fs_organizer/
  __main__.py     CLI entry point and subcommands
  config.py       Schema, validation, effective ignore list
  watcher.py      watchdog handler, organizer decisions, watcher lifecycle
  pool.py         Debouncer and worker pool
  mover.py        Destination rendering, collision-safe moves, cross-volume staging
  rules.py        Extension, sub-rule and name-rule matching
  ai.py           Ollama / OpenAI classifier (stdlib urllib)
  ai_cache.py     Opt-in decision cache
  journal.py      JSONL move journal
  undo.py         Restore from the journal
  duplicates.py   Bounded SHA-256 duplicate scan
  suggest.py      Rule suggestions from uncovered files
  runtime.py      Single-instance guard, pause/resume
  diagnostics.py  check / watch-diag payloads
  ui.py           Local dashboard server
  views.py        Read-only dashboard endpoints
  dashboard.html  Dashboard front end
scripts/          Autostart setup, smoke tests, publish checks
tests/            pytest suite (incl. property and regression tests)
docs/             TESTING.md, RELEASING.md, V2_ROADMAP.md
```

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`docs/TESTING.md`](docs/TESTING.md) — how the suite and smoke tests fit together
- [`docs/RELEASING.md`](docs/RELEASING.md) — publish checklist
- [`docs/V2_ROADMAP.md`](docs/V2_ROADMAP.md) — what shipped and what's next
- [`knowledge.md`](knowledge.md) — architecture cheat sheet

## License

[MIT](LICENSE) © 2026 CoderGamerUnknow
