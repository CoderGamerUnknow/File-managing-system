# fs-organizer V2 Roadmap

V2 goal: grow the V1 rule engine into a **self-observing, user-controllable
organizer daemon** without breaking a single V1 invariant (see
`knowledge.md`). Every phase lands green on the CI matrix
(ubuntu-latest py3.13, windows-latest py3.13, ubuntu-latest py3.9).

Governing skills still apply: low-resource-daemon (memory/CPU budget,
event-driven only), windows-compatibility (paths, locks, signals),
safe-file-operations (never lose/overwrite; failures are safe by design).

## Phase 1 — Runtime control & observability (this release)

The daemon becomes observable and controllable while it runs:

| Feature | What it is | Where |
| --- | --- | --- |
| **Single-instance guard** | A second `fs-organizer` start on the same config exits cleanly with a clear message instead of running two organizers over one folder (double-moves, event storms). Lock-file + pid probe; stale locks are detected and replaced. `--force` overrides deliberately. | `runtime.py` |
| **Runtime stats** | Counters since start: files moved/skipped/refused, AI calls, bytes moved, last-move time. Cheap integer increments on the existing activity feed — no polling, no timers. | `ActivityLog.stats` → `/api/status` |
| **Pause / resume** | Stop acting on files while staying alive: events are still debounced, but the organizer holds them (bounded) until resumed. Dashboard button + `POST /api/pause`. Pausing never drops the loop guards. | `Watcher` |
| **Live config reload** | Re-read the config file at runtime without restarting the daemon: rules, ignore patterns, target root, AI settings. Watch folders may be added/removed. If the new config is invalid, the old one stays active and the error surfaces in the UI. `POST /api/reload` + dashboard button. | `DashboardState`, CLI main loop |
| **Dashboard control strip** | Buttons for Organize now / Pause / Reload config, plus a live stats row (moved, skipped, bytes, uptime). | `dashboard.html` |

## Phase 2 — Smarter organization (planned)

- Per-category sub-rules (e.g. route `*.pdf` differently under `~/Downloads/invoices`)
- File-age policies (never touch files younger than N seconds / older than N days)
- Duplicate detection (hash-based, report-only first)
- Destination templates (`{category}/{YYYY}/{YYYY-MM}/`)

## Phase 3 — Ecosystem (planned)

- Config UI: full rule editor in the dashboard (add/remove/rename categories, edit extensions) with validation and a diff preview before applying
- Windows service host (SCM) alongside the current Run-key autostart
- Optional per-folder `.fs-organizer.json` overrides
- Packaging: `pipx`-friendly extras, portable zip build

## Non-negotiable invariants carried into V2

1. Failures are safe by design; worst case is "nothing happened".
2. Loop guard, scope guard, effective-ignore-list, per-directory move locks: untouched.
3. Idle cost stays ~zero: no new polling loops; the single-instance probe is
   one stat call at startup, pause holds existing debounce slots (no busy wait),
   reload is an on-demand HTTP action, stats are integer increments.
4. The dashboard never binds beyond loopback; POST endpoints stay token-gated.
5. Secrets are redacted in every dump; `api_key` never leaves the process.
6. `python -m pytest` + both smoke scripts must stay green on the CI matrix
   per commit; new features carry their own regression tests.
