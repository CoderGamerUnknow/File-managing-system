# fs-organizer

[![CI](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml/badge.svg)](https://github.com/CoderGamerUnknow/File-managing-system/actions/workflows/ci.yml)

A lightweight, low-resource, event-driven file organizer for Windows 11 (works
anywhere Python runs). It watches your folders — Desktop, Documents,
Downloads, … — and as files appear, sorts them into category subfolders
(`~/Organized/<Category>/`) using extension rules, with an optional AI
fallback for unknown file types. It must run forever at login, cost almost
nothing when idle, never lose or overwrite user data, and never organize its
own output.

## Features

- **Rule-based sorting** — map file extensions to categories (`.pdf` → `Documents`, `.jpg` → `Images`, ...).
- **Watches folders live** — via [watchdog](https://github.com/gorakhargosh/watchdog); new and moved files are picked up automatically.
- **Debounced events** — editors that save in bursts and partial downloads don't trigger half-written moves; a file is only handled once it's been quiet for a configurable grace period.
- **Non-blocking worker pool** — file moves happen on background threads so the watcher never stalls.
- **Collision-safe** — a file that would overwrite an existing one is renamed `name (1).ext`, `name (2).ext`, ...; per-directory locking makes this safe even when two folders are watched at once.
- **Ignore patterns** — glob patterns for file names (no slash, e.g. `*.tmp`) or full paths (contains a slash, e.g. `**/Downloads/**`).
- **Dry-run mode** — log what *would* happen without touching anything.
- **One-shot mode** — `--once` organizes existing files and exits, no watching.
- **AI fallback (optional)** — files with unknown extensions can be classified by OpenAI or a local [Ollama](https://ollama.com) model, strictly limited to an allow-list of categories and extensions.
- **Web dashboard (optional)** — a local-only UI showing live activity, rules, plan preview, pause/reload controls, and a manual "Organize now" button. Zero extra dependencies (stdlib `http.server`).
- **V2 runtime controls** — single-instance guard, pause/resume, live config reload.
- **V2 age policy** — files too new (still being written) or too old (archives) stay put.
- **V2 destination templates** — full control of the organized layout with `{category}` and `{date:FORMAT}` tokens.
- **Stdlib-only AI client** — no SDK dependency; plain `urllib` calls.

## Installation

```bash
pip install .            # installs the fs-organizer command + watchdog
```

or just install the runtime dependency and run from the repo:

```bash
pip install watchdog     # runtime dependency
pip install pytest       # optional, for running the tests
```

Requires Python 3.9+.

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
  "journal": {
    "enabled": false,
    "path": null
  },
  "ai": {
    "enabled": false
  }
}
```

2. Try it without consequences:

```bash
python -m fs_organizer config.json --once --verbose   # set "dry_run": true first to preview
```

In dry-run mode the one-shot scan prints `Dry run: would organize N file(s).` without touching anything.

3. Run the watcher:

```bash
python -m fs_organizer config.json
```

## Configuration reference

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `watch_folders` | list of paths | *(required)* | Folders to watch. Must exist at startup. |
| `target_rules` | object | built-in set | `{ ".ext": "Category" }` mappings. Keys are case-insensitive. |
| `ignore_patterns` | list of globs | *(none)* | Extra globs on top of the built-in set (`.tmp`, `.part`, `.crdownload`, `.*`, …). Globs *without* a slash match the file name only; globs *with* a slash match the full path. The organizer always applies the built-in defaults plus your patterns — the same combined list that `check` prints. |
| `target_root` | path | `~/Organized` | Root folder for the category subfolders. |
| `use_date_subfolders` | bool | `false` | Also group by file mtime: `Documents/2026-10/`. Legacy; superseded by `destination_template` (which wins if both are set). |
| `destination_template` | object | `{category}` | **V2.** Where organized files land: `{"pattern": "{category}/{date:%Y}/{date:%Y-%m}"}` → `Organized/Documents/2026/2026-10/a.txt`. Tokens: `{category}` (required) and `{date:FORMAT}` (strftime on the file's mtime; defaults to `%Y-%m`). Rendered relative to `target_root`; absolute patterns and patterns without `{category}` are rejected; malformed date formats fall back to `%Y-%m` instead of crashing a move. |
| `age_policy` | object | *(disabled)* | **V2.** `{"min_age_seconds": 0, "max_age_days": null}`. Files younger than `min_age_seconds` (still being written — a second line of defense behind the debouncer) or older than `max_age_days` (archives, old mailboxes) stay where they are. Both bounds are evaluated against the file's mtime; `0` / `null` disable each bound. Plans and diagnostics report skipped files in their own `would_skip_age` bucket — never as a move the organizer won't make. |
| `dry_run` | bool | `false` | Log intended moves without moving anything. |
| `file_stable_seconds` | number | `1.0` | How long a file must be quiet before it is moved. |
| `recursive` | bool | `false` | **Opt-in.** Also watch and organize all *subfolders* of every watch folder. Off by default: the top-level-only scope is safer. When enabled, everything changes together — the live watcher, `--once`, plans, and diagnostics all cover the deeper scope, and the organizer's own target-root subtree stays excluded. |
| `journal` | object | `enabled: false` | Persisted move journal: an append-only JSONL audit trail (`ts`, `src`, `dest`, `category`, `size` per move). Its `ts` is also the authoritative creation date the dashboard shows for organized files — on filesystems without birthtime the mtime would otherwise mislabel every moved file with the move time. Reads tolerate a torn final line (crash mid-append). Recording is best-effort and never breaks a move. |
| `journal.path` | path | `~/.fs-organizer/moves.jsonl` | Where the journal is written (created on first move). |
| `ai` | object | see below | Optional AI fallback configuration. |

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
  "allowed_subfolders": ["Documents", "Images", "Other"]
}
```

- **provider** — `ollama` (local, default) or `openai`.
- **api_key** — required for `openai` (either here or via the `OPENAI_API_KEY` env var).
- **extensions** — only files with these extensions are sent to the model.
- **allowed_subfolders** — the model may only pick from these categories (matched case-insensitively); anything else is ignored and the file is left in place. Required when `enabled` is true. The model reads a truncated preview of the file (text, lossily decoded).

Failures are safe by design: network errors, malformed replies, or disallowed
categories leave the file untouched — worst case, nothing happens. The API
key is redacted in every config dump (`check --json`, `watch-diag --json`).

## CLI

```
fs-organizer CONFIG [--once] [-v/--verbose] [--log-file F] [--ui] [--port N] [--no-browser] [--force] [--watch-files PATH]
fs-organizer CONFIG check [--json] [--watch-files PATH]
fs-organizer CONFIG watch-diag [--json] [--watch-files PATH]
```

- `CONFIG` — path to the JSON config file.
- `--once` — organize existing files and exit (also honors `dry_run`).
- `-v` — debug logging.
- `--log-file F` — also write logs to this file.
- `--ui` — serve the web dashboard while watching (watch mode only).
- `--port N` — dashboard port (default `8765`; `0` picks a free port).
- `--no-browser` — with `--ui`: don't open the browser automatically.
- `--force` — start even if another daemon instance holds this config's lock (exit code 3 without it).
- `--watch-files PATH` — with `check` / `watch-diag`: report the ignore verdict for any specific path (repeatable) without walking it.

### `check` — config + plan preview

Prints the resolved config (redacted), the effective ignore list, and a
bounded plan of what would happen to every candidate file, grouped by
decision: `would_organize` (rule matched), `would_classify_ai` (AI-able
extension), `would_skip_pattern` (ignored), `would_skip_age` (V2 age policy),
and `would_skip_unknown` (no rule, left alone). Add `--json` for the
machine-readable payload.

### `watch-diag` — why is my file not moving?

Per watch folder: how many files are ignored (and how many of those would
otherwise match a rule), how many have no rule and are not AI-able, and the
exact effective ignore patterns. The per-file companion: pass
`--watch-files some/path` to get the ignore verdict for any single path.

## Web dashboard

Run `python -m fs_organizer config.json --ui` and open http://127.0.0.1:8765.

The dashboard shows:

- **Status** — target root, stability window, dry-run badge, AI fallback summary.
- **Watched folders** and the **rule table** (category → extensions).
- **Recent activity** — every moved / skipped / dry-run decision the watcher makes, live. Events carry a gap-free monotonic `seq`; the UI detects a ring-buffer wrap or counter reset and re-syncs from a full snapshot, so incremental polling never misses or duplicates events.
- **Plan preview** — every candidate with its deciding category (the rule/AI
  category, never a path-derived lookalike like the `YYYY-MM` folder) and its
  template-rendered destination.
- **Files by date** — every organized file grouped by its creation date (newest day
  first), with **month/year navigation** (dropdown + ‹ › steppers), per-day summaries
  (file count, total size, per-category breakdown), per-file sizes in human-readable
  units, and sorting by creation time or name. On Windows the file's creation time is
  used; on filesystems without birthtime support the last-modified time is shown instead.
- **Controls** — **Organize now** (one-shot scan), **Pause/Resume** (holds
  incoming events while paused — bounded by the debouncer — and dispatches
  them on resume), **Reload config** (re-reads the config file live; a
  rejected reload keeps the old config active and reverts the watches), and
  the single-instance indicator.

It binds to `127.0.0.1` only (never exposed to the network), and its mutating
endpoints (`POST /api/once`, `/api/pause`, `/api/reload`) require a
per-process random token that is injected into the served page. The read-only
`GET` endpoints expose only paths and metadata under your configured
watch/target folders. No extra dependencies — just the standard library. An
end-to-end check lives at `scripts/smoke_ui.py`.

## How it works

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
                          moves.jsonl): audit trail + dashboard creation-date
```

1. watchdog raises filesystem events for created/moved files.
2. The **debouncer** waits until the path has been quiet for `file_stable_seconds`.
3. The **worker pool** runs the organizer for that path on a background thread — duplicate events for the same file that land while it is queued or being handled are coalesced, so a file is never moved twice.
4. The **organizer** ignores matching patterns, skips anything inside the target
   root (no infinite loops), checks the V2 age policy, resolves a category from
   the rules (or the AI fallback), and **moves** the file — creating the
   destination folder as needed (rendered from `destination_template`) and
   suffixing `(1)`, `(2)`, ... on collisions.
5. When the journal is enabled, the move is appended to the JSONL audit trail.

### Safety invariants

1. Failures are safe by design: worst case is "nothing happened", never data loss.
2. Never touch anything inside the target root (loop guard) or outside the
   watched roots (scope guard).
3. Every long-lived thread is a daemon and is stopped by `Watcher.stop()`,
   which drains pending debounced files and queued work first.
4. All config validation lives in `config.load_config`; errors are
   `ConfigError` (exit code 2 from the CLI).
5. `classify_with_ai` never raises; unknown/disallowed AI answers leave the file in place.
6. One lock per *destination directory* serializes the existence-check +
   move sequence — two workers moving same-named files can never overwrite each other.
7. The dashboard must never bind beyond loopback, and its HTML must escape
   user-influenced strings (file names) before rendering.
8. The ignore decision is always `Config.effective_ignore_patterns()` —
   built-in defaults + user list — everywhere (mover, watcher, one-shot,
   diagnostics). No consumer uses the raw user list.

## V2 runtime controls

- **Single-instance guard** (`runtime.py`) — one daemon per resolved config
  path. A second start exits with code 3 unless `--force`. Stale locks (dead
  pid) are replaced automatically — no manual cleanup. Different configs may
  run side by side.
- **Pause / Resume** — pause HOLDS events (bounded by the debouncer queue),
  never drops them; `stop()` releases held paths before draining.
- **Live config reload** — the dashboard re-reads the config file. The swap
  happens through a single source of truth (`DashboardState.config`); a
  rejected reload keeps the old config active and fully reverts the watcher's
  watches on a failed apply.

## Start automatically at login (Windows)

Run the one-time installer:

```bash
python scripts/setup_autostart.py
```

It will:

1. Create a live config at `~\.fs-organizer\config.json` that watches your real
   user folders (Desktop, Documents, Downloads, Pictures, Music, Videos —
   including OneDrive-redirected locations, resolved from the registry).
2. Register a `HKCU\...\Run` entry so `pythonw.exe -m fs_organizer <config>`
   starts **hidden in the background** every time you log in. No admin rights,
   no console window.
3. Write all activity to `~\.fs-organizer\fs-organizer.log`. The log rotates
   automatically (1 MB per file, 2 backups kept), so it never grows unbounded.

Files are organized into `~/Organized/<Category>/` as they appear. To remove
the auto-start entry later:

```bash
python scripts/setup_autostart.py --remove
```

The run key only starts the program at login — to apply config changes
immediately, kill the running `pythonw` process and start it again (or use the
dashboard's **Reload config** button).

## Running the tests

```bash
python -m pytest
python scripts/smoke_dryrun.py   # e2e: dry-run + graceful shutdown
python scripts/smoke_ui.py       # e2e: web dashboard reflects live activity
```

CI runs the suite plus both smoke tests on Ubuntu + Windows (Python 3.13, and
3.9 on one runner to guard the floor). On failure it publishes the pytest and
smoke logs to a `ci-logs-<os>-py<ver>` branch and as workflow artifacts, so a
red run is always diagnosable without re-running anything.

## License

MIT
