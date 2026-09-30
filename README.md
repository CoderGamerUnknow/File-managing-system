# fs-organizer

A lightweight, rule-based file organizer that watches your folders and sorts
new files into category subfolders — with an optional AI fallback for file
types your rules don't cover.

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
- **Web dashboard (optional)** — a local-only UI at `http://127.0.0.1:8765` showing live activity, rules, and a manual "Organize now" button. Zero extra dependencies (stdlib `http.server`).
- **Stdlib-only AI client** — no SDK dependency; plain `urllib` calls.

## Installation

```bash
pip install watchdog          # runtime dependency
pip install pytest            # optional, for running the tests
```

or install the package itself:

```bash
pip install .
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
| `use_date_subfolders` | bool | `false` | Also group by file mtime: `Documents/2026-09/`. |
| `ai.allowed_subfolders` | list | `[]` | Categories the model may pick from. Required (non-empty) when `ai.enabled` is true; matching is case-insensitive. |
| `dry_run` | bool | `false` | Log intended moves without moving anything. |
| `file_stable_seconds` | number | `1.0` | How long a file must be quiet before it is moved. |
| `recursive` | bool | `false` | **Opt-in.** Also watch and organize all *subfolders* of every watch folder (foldered Downloads, etc.). Off by default: the top-level-only scope is safer. When enabled, everything changes together — the live watcher, `--once`, plans, and diagnostics all cover the deeper scope, and the organizer's own target-root subtree stays excluded. |
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
categories leave the file untouched — worst case, nothing happens.

## CLI

```
fs-organizer CONFIG [--once] [-v/--verbose] [--log-file F] [--ui] [--port N] [--no-browser]
```

- `CONFIG` — path to the JSON config file.
- `--once` — organize existing files and exit (also honors `dry_run`).
- `-v` — debug logging.
- `--ui` — serve the web dashboard while watching (watch mode only).
- `--port N` — dashboard port (default `8765`; `0` picks a free port).
- `--no-browser` — with `--ui`: don't open the browser automatically.

## Web dashboard

Run `python -m fs_organizer config.json --ui` and open http://127.0.0.1:8765.

The dashboard shows:

- **Status** — target root, stability window, dry-run badge, AI fallback summary.
- **Watched folders** and the **rule table** (category → extensions).
- **Recent activity** — every moved / skipped / dry-run decision the watcher makes, live.
- **Files by date** — every organized file grouped by its creation date (newest day
  first), with **month/year navigation** (dropdown + ‹ › steppers), per-day summaries
  (file count, total size, per-category breakdown), per-file sizes in human-readable
  units, and sorting by creation time or name. On Windows the file's creation time is
  used; on filesystems without birthtime support the last-modified time is shown instead.
- **Organize now** — runs a one-shot scan over the watch folders immediately.

It binds to `127.0.0.1` only (never exposed to the network), and its one
mutating endpoint (`POST /api/once`) requires a per-process random token that
is injected into the served page. The read-only `GET` endpoints expose only
paths and metadata under your configured watch/target folders. No extra
dependencies — just the standard library. An end-to-end check lives at
`scripts/smoke_ui.py`.

## How it works

1. watchdog raises filesystem events for created/moved files.
2. The **debouncer** waits until the path has been quiet for `file_stable_seconds`.
3. The **worker pool** runs the organizer for that path on a background thread — duplicate events for the same file that land while it is queued or being handled are coalesced, so a file is never moved twice.
4. The **organizer** ignores matching patterns, skips anything inside the target
   root (no infinite loops), resolves a category from the rules (or the AI
   fallback), and **moves** the file — creating the category folder as needed
   and suffixing `(1)`, `(2)`, ... on collisions.

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
immediately, kill the running `pythonw` process and start it again.

## Running the tests

```bash
python -m pytest
python scripts/smoke_dryrun.py   # e2e: dry-run + graceful shutdown
python scripts/smoke_ui.py       # e2e: web dashboard reflects live activity
```

## License

MIT
