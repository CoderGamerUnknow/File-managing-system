<div align="center">

# fs-organizer

**A low-resource, event-driven file organizer. It watches your folders and files new
arrivals into category subfolders — safely, and without ever losing data.**

[![CI](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml/badge.svg)](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3772ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-3b82f6?style=flat-square)](LICENSE)

</div>

## Overview

Built for Windows 11 but runs anywhere Python does. It starts at login, costs almost
nothing while idle, and files anything that appears in a watched folder (Desktop,
Documents, Downloads, …) into `~/Organized/<Category>/` using extension rules, with an
optional local-AI fallback for unknown types.

Three constraints shape the whole codebase: **never lose or overwrite user data, never
organize its own output, and never stall the watcher.** Every failure mode is designed to
end in "nothing happened" rather than a half-moved file.

## Features

**Sorting**
- Extension → category rules (`.pdf` → `Documents`), plus ordered **sub-rules** that route
  by source folder and **name rules** that route by glob or regex — sub-rules first, then
  name rules, then the global extension table.
- Destination templates: `{category}` and `{date:FORMAT}` control the organised layout.
- **Age policy** — files still being written and old archives stay put.
- **Quiet hours** — organise only inside a daily `HH:MM` window (midnight wrap included).
- **Ignore patterns** — globs for names (`*.tmp`) or full paths (`**/Downloads/**`).

**Safety**
- **Collision-safe moves** — `name (1).ext`, `name (2).ext`, … with one lock per
  destination directory, so two workers can never overwrite each other.
- Cross-volume moves are staged (copy → fsync → rename → delete) and SHA-256 verified
  before the source is removed; a disk-space guard refuses moves onto a nearly-full volume.
- **Undo** — recent moves are replayed from the journal, never overwriting a re-occupied
  original. Nothing is ever deleted, including duplicate quarantine.
- **Dry-run** and `--once` one-shot modes; single-instance guard; loop guard keeps the
  target root out of scope.

**Runtime**
- Live `watchdog` monitoring with a per-path debounce (`file_stable_seconds`) so burst
  saves and partial downloads don't trigger half-written moves.
- Two daemon workers with a bounded queue; duplicate events coalesce into one task.
- Pause/resume, live config reload, and an optional local dashboard.

**Analysis & AI**
- **Duplicate scan** — report-only SHA-256 groups with a size pre-filter, streamed hashing
  and a bounded walk.
- **Quarantine** — moves every duplicate copy except the oldest into `_Duplicates/`.
- **Suggest** — mines watch folders and the journal for uncovered files and prints
  paste-ready rule snippets.
- **Journal export** (CSV/JSON) and an optional `ollama`/`openai` classifier that can only
  choose from categories you allow, with an opt-in decision cache.

## Tech stack

Python 3.9+ and [`watchdog`](https://github.com/gorakhargosh/watchdog) — the only runtime
dependency. The dashboard, duplicate scan, AI client (plain `urllib`) and CLI are
stdlib-only.

## Getting started

```bash
pip install .            # installs the fs-organizer command + watchdog
pip install pytest       # optional, for the tests
```

1. Copy `config.example.json` to `config.json` and edit the watch folders:

```json
{
  "watch_folders": ["~/Downloads"],
  "target_root": "~/Organized",
  "target_rules": { ".pdf": "Documents", ".jpg": "Images", ".zip": "Archives" },
  "ignore_patterns": [".*", "*.tmp", "*.part", "*.crdownload"],
  "dry_run": false,
  "file_stable_seconds": 1.0,
  "recursive": false
}
```

2. Preview it (set `"dry_run": true` first) — nothing is touched:

```bash
python -m fs_organizer config.json --once --verbose
# Dry run: would organize N file(s).
```

3. Run the watcher:

```bash
python -m fs_organizer config.json              # watch
python -m fs_organizer config.json --once       # organise what exists, then exit
python -m fs_organizer config.json --ui         # watch + dashboard on 127.0.0.1:8765
```

`fs-organizer init` writes a starter config for the user folders that exist on your
machine, with `dry_run: true` already set (refuses to overwrite without `--force`).

## Commands

```
fs-organizer CONFIG [--once] [-v] [--log-file F] [--ui] [--port N] [--no-browser] [--force] [--watch-files PATH]
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

| Command | What it does |
| --- | --- |
| `check` | Resolved config (redacted) + a bounded plan per decision: `would_organize`, `would_classify_ai`, `would_skip_pattern`, `would_skip_age`, `would_skip_unknown` |
| `watch-diag` | Why a file isn't moving: ignored counts per folder, files with no rule, effective ignore list |
| `dupes` | Report-only SHA-256 duplicate groups. Unique sizes are never read; hashing streams in 1 MiB chunks; `--max-files` (2000) / `--max-mb` (256) bound the walk and report `truncated: true` |
| `undo [N]` | Restores the N most recent moves (default 1) from the journal; a re-occupied original becomes `name (1).ext` |
| `suggest` | Read-only: uncovered extensions by file count, journal activity per category, unused AI categories |
| `quarantine` | Moves duplicate copies (all but the oldest) to `<target_root>/_Duplicates/<date>/<hash>/` |
| `export` | Journal dump, CSV (default) or JSON; also `GET /api/export?format=csv` |
| `init` | Starter config for your real user folders, `dry_run: true`, no overwrite without `--force` |

| Flag | Effect |
| --- | --- |
| `--once` | Organise existing files and exit (honours `dry_run`) |
| `-v` / `--verbose` | Debug logging |
| `--log-file F` | Also write logs to a file |
| `--ui` | Serve the dashboard while watching (`--port`, `--no-browser`) |
| `--force` | Start despite another instance holding this config's lock (otherwise exit code 3) |
| `--watch-files PATH` | With `check`/`watch-diag`: the verdict for one path, without walking it |

Environment: `FSORG_DRY_RUN` forces preview mode for one process, `FSORG_TARGET_ROOT`
redirects the destination, `OPENAI_API_KEY` supplies the `openai` provider key.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `watch_folders` | *(required)* | Folders to watch; must exist at startup |
| `target_rules` | built-in set | `{".ext": "Category"}`, case-insensitive extensions |
| `target_root` | `~/Organized` | Root for the category subfolders |
| `ignore_patterns` | *(none)* | Globs added to the built-in set (no slash → file name, with slash → full path) |
| `destination_template` | `{category}` | e.g. `{category}/{date:%Y}/{date:%Y-%m}`; `{category}` is required, malformed dates fall back to `%Y-%m` |
| `use_date_subfolders` | `false` | Legacy `Documents/2026-10/` grouping, superseded by `destination_template` |
| `sub_rules` | *(none)* | `{"pattern": "~/Downloads/invoices/**", "extensions": [".pdf"], "category": "Invoices"}` — ordered, first match wins |
| `name_rules` | *(none)* | `{"pattern": "invoice*", "category": "Invoices"}` or `{"pattern": "^IMG_\\d+", "category": "Photos", "regex": true}` |
| `age_policy` | *(disabled)* | `{"min_age_seconds": 0, "max_age_days": null}`; skips are reported, never planned |
| `quiet_hours` | *(disabled)* | `{"start": "22:00", "end": "06:00"}` |
| `disk_space_guard` | `0.0` | Minimum free-space percentage on the destination volume (`0` = off) |
| `dry_run` | `false` | Log intended moves without moving |
| `file_stable_seconds` | `1.0` | Quiet period before a file moves |
| `recursive` | `false` | Watch subfolders too; the target-root subtree stays excluded |
| `journal` | `enabled: false` | Append-only JSONL audit trail at `~/.fs-organizer/moves.jsonl`; best-effort, never breaks a move |
| `ai` | *(disabled)* | Optional classifier (below) |

Invalid config exits with code 2; secrets are redacted in every dump.

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
  "cache_enabled": false
}
```

Only listed extensions are sent, only listed categories may be chosen, and the model sees
a truncated preview. Network errors, malformed replies and disallowed categories leave the
file where it is. `cache_enabled` memoises successes at `~/.fs-organizer/ai_cache.json`
(never failures, so an outage can't poison it). `provider: "openai"` needs an `api_key` or
`OPENAI_API_KEY`.

## Web dashboard

```bash
python -m fs_organizer config.json --ui   # http://127.0.0.1:8765
```

Status, watched folders, the rule table, live activity (gap-free monotonic `seq` with
ring-buffer resync), duplicate groups, files grouped by creation date, and controls:
**Organize now**, **Pause/Resume** (holds events, never drops them), **Reload config**
(a rejected reload keeps the old config and reverts the watches), **Undo last**, **Export**.
A debounced client-side filter searches by name or category.

The server binds to `127.0.0.1` only; mutating endpoints require a per-process random token
injected into the served page, and file names are escaped before rendering. `/api/status`
is polled every 2 s and stays cheap; the plan preview is on demand at `GET /api/plan`.

## How it works

```
watchdog events
      │
      ▼
watcher._EventHandler ── schedules only paths inside watched roots
      │
      ▼
pool.Debouncer ──────── per-path quiet window (file_stable_seconds)
      │
      ▼
pool.WorkerPool ─────── 2 daemon workers, bounded queue; duplicate events coalesce
      │
      ▼
watcher.Organizer ───── ignores → target-root guard → age policy → rules → AI
      │
      ▼
mover.move_file ─────── destination render + collision-safe rename + per-dir lock;
      │                  cross-volume moves staged and verified
      ▼
journal.py ──────────── append-only JSONL (optional)
```

**Safety invariants**

1. The worst case is "nothing happened" — never data loss.
2. Never touch anything inside the target root, or outside the watched roots.
3. Every long-lived thread is a daemon and is drained by `Watcher.stop()`.
4. All validation lives in `config.load_config` and raises `ConfigError` (exit code 2).
5. `classify_with_ai` never raises; unusable answers leave the file in place.
6. One lock per destination directory covers the existence check plus the move.
7. Every consumer uses `Config.effective_ignore_patterns()` — defaults plus user globs.

## Project structure

```
fs_organizer/
  __main__.py     CLI entry point and subcommands
  config.py       Schema, validation, effective ignore list
  watcher.py      watchdog handler, organizer decisions, lifecycle
  pool.py         Debouncer and worker pool
  mover.py        Destination rendering, collision-safe moves
  rules.py        Extension, sub-rule and name-rule matching
  ai.py           Ollama / OpenAI classifier (stdlib urllib)
  ai_cache.py     Opt-in decision cache
  journal.py      JSONL move journal
  undo.py         Restore from the journal
  duplicates.py   Bounded SHA-256 duplicate scan
  suggest.py      Rule suggestions from uncovered files
  runtime.py      Single-instance guard, pause/resume
  diagnostics.py  check / watch-diag payloads
  ui.py, views.py Local dashboard server and read-only endpoints
  dashboard.html  Dashboard front end
scripts/          Autostart setup, smoke tests, publish checks
tests/            pytest suite (incl. property and regression tests)
docs/             TESTING.md, RELEASING.md, V2_ROADMAP.md
```

## Start at login (Windows)

```bash
python scripts/setup_autostart.py           # add
python scripts/setup_autostart.py --remove  # remove
```

Creates a live config at `~\.fs-organizer\config.json` from your real user folders
(including OneDrive redirects, resolved from the registry), registers an
`HKCU\...\Run` entry so `pythonw.exe -m fs_organizer <config>` starts hidden at every login
(no admin rights, no console), and rotates logs at `~\.fs-organizer\fs-organizer.log`
(1 MB × 2). The run key only applies at login — restart the process or use **Reload config**
for immediate changes.

## Tests

```bash
python -m pytest
python scripts/smoke_dryrun.py   # e2e: dry-run + graceful shutdown
python scripts/smoke_ui.py       # e2e: dashboard reflects live activity
```

CI runs the suite plus both smoke tests on Ubuntu and Windows (Python 3.13, with 3.9 on one
runner to guard the floor), and publishes pytest/smoke logs to a `ci-logs-<os>-py<ver>`
branch and as artifacts when a run fails.

## More documentation

[`CHANGELOG.md`](CHANGELOG.md) · [`docs/TESTING.md`](docs/TESTING.md) ·
[`docs/RELEASING.md`](docs/RELEASING.md) · [`docs/V2_ROADMAP.md`](docs/V2_ROADMAP.md) ·
[`knowledge.md`](knowledge.md)

## License

[MIT](LICENSE) © 2026 CoderGamerUnknow
