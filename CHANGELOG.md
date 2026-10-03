# Changelog

All notable changes to fs-organizer are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Concurrent same-name moves could silently overwrite a file** (flaw #51).
  The per-destination-directory lock — the guard that makes the
  check-then-move sequence safe against two workers (flaw #1) — was keyed
  by the raw `Path` object. Windows `Path.resolve()` intermittently returns
  the extended-path form (`\\?\C:\...`) depending on what exists at resolve
  time, so one destination directory could end up with **two different lock
  objects**: the worker that resolved before the directory existed and the
  worker that resolved after took different locks, both passed the "is this
  name free?" check, and `shutil.move`'s `os.rename` replaced the first
  file. Reproduced on Windows in ~60% of 8-thread runs: only 6–7 of 8
  same-named files survived while **every call still reported
  `moved=True`**. The lock map is now keyed by the normalized path string
  (the same `_normalize_for_compare` canonicalization the containment guard
  uses, which already strips the `\\?\` prefix), and `move_file` resolves the
  destination again *after* `mkdir` so every worker keys the same canonical
  path. Verified with 12/12 clean 8-thread stress runs after the fix. The
  pre-fix and post-fix versions both pass the ordinary suite — only a
  concurrent test exposes this.

### Testing

- New `tests/test_mover_properties.py`: property-based tests for the
  mover's safety invariants — no-overwrite under collisions, destination
  containment for arbitrary category strings, the loop guard at the
  organizer, dry-run immutability, ignore/age-policy decisions, and
  "never raises" for pathological inputs — each driven by a deterministic
  seeded generator (so failures reproduce) plus optional deeper
  hypothesis-generated variants when `hypothesis` is installed. Hypothesis is
  deliberately NOT a project dependency: those four generated tests are
  dormant (reported as a skip) until someone installs it, so CI exercises the
  seeded properties only. Alongside
  them, fault-injection tests attack the failure paths directly: a copy that
  dies half-way, a rename that fails, an fsync that fails, a source delete
  that fails after the commit, a WinError 32 lock, an uncreatable
  destination directory, an unwritable journal, and the 8-thread same-name
  race.
- New `TestFlaw51DestinationLockKey` in `tests/test_flaw_regressions.py`:
  deterministic (thread-free) regression tests pinning the lock-key
  canonicalization, including the plain vs `\\?\` spelling case.

## [0.4.0] — 2026-10-03

The safety-valve + control release: undo, quarantine-based duplicate
cleanup, name-based and time-window rules, and a first pass at making the
daemon scriptable (env overrides, journal export, `init`). Every mutating
feature is opt-in and never deletes anything; everything else is additive
and safe by default. Existing configs keep their exact behavior.

### Added

- **`undo` — the journal, run backwards** (`fs_organizer/undo.py`,
  `fs-organizer CONFIG undo [N] [--dry-run] [--json]`, dashboard **Undo
  last** button / `POST /api/undo`). Reads the move journal and restores the
  most recent organized files to where they came from. It inherits the
  forward mover's guarantees rather than inventing weaker ones: a
  re-occupied original gets the collision suffix (`a (1).txt`) instead of
  being overwritten, a destination that no longer exists is reported and
  skipped (nothing else is touched), a `src` that would land somewhere
  insane from a hand-edited journal is refused, and `--dry-run` previews the
  batch without moving anything. `count` bounds the batch (newest first);
  the dashboard endpoint is token-protected, clamps `count` to 0–500, and
  returns 409 with a clear message when the journal is disabled — there
  would be nothing to undo, so fail loudly instead of pretending. The
  journal itself is never rewritten: it stays a faithful log of forward
  moves, and undo is itself just moves (re-runnable, redoable).

- **`quarantine` — actionable duplicate cleanup** (`quarantine_duplicates`
  in `fs_organizer/duplicates.py`, `fs-organizer CONFIG quarantine
  [--dest PATH] [--dry-run] [--json]`). The opt-in counterpart to the
  report-only `dupes` scan: for each group of identical files the oldest
  copy stays put (the presumed original) and every other copy is moved to
  `<target_root>/_Duplicates/<YYYY-MM-DD>/<hash[:12]>/` with the usual
  collision suffixing. **Nothing is ever deleted** — the operation is fully
  reversible by hand or with `undo` — which keeps the safe-file-operations
  invariant intact while making duplicates actually removable from your
  working set. Files that vanish between scan and quarantine are reported
  in `skipped`, never fatal. `--dry-run` (or `dry_run: true`) lists what
  would move. Same-volume moves use `shutil.move`; cross-volume uses the
  staged path.

- **Name rules (`name_rules`)** — route by file *name*, not just extension:
  `{"pattern": "invoice*", "category": "Invoices"}` or
  `{"pattern": "^IMG_\\d+", "category": "Photos", "regex": true}`, with an
  optional `extensions` list to narrow the rule to specific suffixes
  (case-insensitive on both sides). Evaluated in config order, first match
  wins, at a fixed precedence: `sub_rules` (source folder) → `name_rules`
  (file name) → the global `target_rules` table. All three are consulted by
  the single classification entry point `Config.category_for()`, so the
  watcher, `--once`, plans, and diagnostics always agree. Invalid rules
  (missing/blank pattern or category, bad regex, extensions without a dot,
  non-object entries) are rejected at load with `ConfigError` (exit 2).
  Exported through `to_dict()` → visible in `check --json` and
  `/api/status`.

- **Quiet hours (`quiet_hours`)** — organize only inside a daily window:
  `{"start": "22:00", "end": "06:00"}` (end ≤ start wraps midnight; both
  unset = disabled). Outside the window the live organizer simply does not
  act — files are *left in place*, not held, so anything arriving at night
  is picked up by the next event or scan after the window opens. Validated
  as `HH:MM` at load; the pure window logic is exercised with explicit
  timestamps so the tests never sleep.

- **AI decision cache (`ai.cache_enabled`)** (`fs_organizer/ai_cache.py`).
  Successful classifications are memoized in a JSON file (default
  `~/.fs-organizer/ai_cache.json`, override with `ai.cache_path`) keyed by
  a SHA-256 over provider + model + extension + a streamed hash of the
  exact preview window the model sees — switching models never serves a
  stale answer, and two files that would produce the same prompt share an
  entry. Repeat files (`.dat` exports, form downloads) never hit the model
  again: a cache hit returns before any network call. Writes are atomic
  (temp + `os.replace`) under a lock; a corrupt/unwritable cache degrades
  to "no cache"; **only successful answers are cached**, so a transient
  outage can never poison it. Off by default (it writes a file).

- **`suggest` — rule suggestions from reality** (`fs_organizer/suggest.py`,
  `fs-organizer CONFIG suggest [--json] [--max-files N]`). Scans the watch
  folders (bounded) for files that matched no rule and are not AI-able, and
  proposes ready-to-paste config snippets ordered by how many files each
  extension accounts for; also summarizes journal activity per category
  and lists AI-allowed categories that neither a rule nor the journal has
  ever used. Pure reporting — no config is written.

- **Journal export** — `fs-organizer CONFIG export [--format csv|json]` and
  a loopback-only `GET /api/export?format=csv|json` on the dashboard
  (served as an attachment; read-only like every other GET, so a browser
  download needs no token). CSV columns:
  `ts,ts_iso,src,dest,category,size` (epoch + ISO-8601 local time side by
  side). A disabled or missing journal exports an empty document instead
  of failing.

- **`fs-organizer CONFIG init`** — writes a starter config for the folders
  that actually exist in your home directory (Desktop, Documents,
  Downloads, …), with `dry_run: true` already set. Refuses to overwrite an
  existing file unless `--force`; `--output PATH` picks the destination.
  Dispatched *before* config load (the file usually does not exist yet),
  and missing parent directories are created.

- **Environment overrides for scripting/CI** — `FSORG_DRY_RUN`
  (`1/true/yes/on`) forces preview mode and `FSORG_TARGET_ROOT` redirects
  the output root for that process only; nothing is written back. File
  values remain the defaults when the variables are unset.

- **Disk-space guard (`disk_space_guard`)** — refuse to move when the
  destination volume's free percentage is below the threshold (0 = off,
  the default). A nearly-full volume is where a move becomes a
  half-copied file; refusing turns that into "nothing happened". A volume
  that cannot be queried (unusual filesystems, permissions) is treated as
  OK — failing closed would strand files forever.

- **Staged-move integrity verification** — the cross-volume path
  (copy → fsync → *verify* → rename → delete) now re-hashes the staged
  `.part` file against the source before the atomic rename; a size or hash
  mismatch aborts the commit, removes the temp, and leaves the source
  untouched. The source is only ever deleted after the destination is
  provably complete.

- **Dashboard: file filter, export, and undo** — a debounced search box in
  the Files card filters the list by name or category (client-side, so no
  extra server scan; day headers reflect the visible rows while filtering),
  an **Export** button downloads the journal as CSV, and **Undo last**
  restores the most recent move via the token-protected `POST /api/undo`
  (the button surfaces the server's reason on failure, e.g. a disabled
  journal) and refreshes the file list afterward.

### Changed

- `NameRule.matches()` compares extensions case-tolerantly on both sides,
  so a hand-built rule (`extensions: [".PDF"]`) behaves like the same rule
  loaded through `from_dict` (which normalizes to lowercase).
- Project lint policy is now explicit: `[tool.ruff]` in `pyproject.toml`
  (target py39, line-length 100, first-party isort, rule set
  `E4/E7/E9/F/I/UP/B/BLE/C4/SIM/RUF`), `ruff>=0.6` in the `dev` extra, and
  a **Lint (ruff)** step in CI before the test job. Previously a
  user-level ruff config leaked into the repo and made lint results
  non-deterministic; `ruff check .` is now clean and enforced.

### Fixed

- Several `# noqa: BLE001` markers removed: BLE001 does not fire on
  logged broad-excepts, so the suppressions were dead weight.
- Duplicate `_fmt_size` helper in `tests/test_mover_plan.py` removed (the
  real one is imported from `fs_organizer.views`).
- `pytest.raises(..., match=...)` patterns containing `.` now use raw
  strings (RUF043) so the metacharacter matches literally.

### Testing

481 tests pass, plus `scripts/smoke_dryrun.py` and `scripts/smoke_ui.py`.
New coverage: `tests/test_v3_features.py` (94 tests — name-rule parsing /
matching / precedence, quiet-hours windows incl. midnight wrap and the
live organizer's defer behavior, env overrides, the AI cache incl.
"network must not be touched on a hit" and "failures are never cached",
undo round trips / collision / dry-run / count bounds, suggestions,
quarantine / dry-run / vanish-between-scan-and-move, journal export, the
`_verify_copy` / `_staged_move` abort path, the disk-space guard, and every
new CLI subcommand) plus 14 new dashboard endpoint tests (export download,
token-gated undo, count validation, page affordances). The flaw #50
`watch-diag` CLI tests moved from `tests/test_cli.py` into
`TestFlaw50WatchDiagCli` in `tests/test_flaw_regressions.py` so the
regression file's numbering stays self-contained.

## [0.3.1] — 2026-10-03

Feature + bug-fix release: per-category sub-rules, report-only duplicate
detection, and four correctness/safety fixes. Both new features are opt-in and
default-off, so existing configs keep their exact behavior and the upgrade is
drop-in.

### Added

- **Per-category sub-rules (`sub_rules`)**. Route files differently by source
  folder instead of only by extension:
  `{"pattern": "**/invoices/**", "extensions": [".pdf"],
  "category": "Invoices"}` sends PDFs under a `Downloads/invoices/` folder to
  `Invoices` while every other PDF still follows the global `target_rules`
  table. Matching is extension-first — the file's suffix must appear in the
  rule's `extensions` list (case-insensitive, normalized to lowercase at load)
  **and** its source path must match the rule's glob, using the same
  slash convention as `ignore_patterns` (a pattern containing `/` matches the
  full path; a bare pattern matches the file name only). A leading `~` is
  expanded to the home directory exactly as it is for `watch_folders` and
  `target_root`, so `~/Downloads/invoices/**` works — the common form is
  `{"pattern": "~/Downloads/invoices/**", "extensions": [".pdf"],
  "category": "Invoices"}`. Expansion is deliberately narrow: only a bare
  `~` or a `~` immediately followed by a separator counts, because
  `os.path.expanduser("~scan.pdf")` returns `C:\Users\scan.pdf` on Windows
  (CPython reads everything after a lone `~` as a username), which would
  silently rewrite a legal file-NAME glob. So `~scan.pdf` keeps matching the
  literal file name, and `~someoneelse/x` stays unexpanded rather than
  resolving to another account's home. Backslashes are normalized to forward
  slashes for matching (the path form `fnmatch` sees), so a Windows-style
  `~\\Downloads\\**` works too. Note `**` is not special to `fnmatch` — it
  is two ordinary `*` wildcards. The authored `pattern` is stored verbatim,
  so `check --json` and any config written back from a dump still show the
  user's own text rather than an absolute home path. Rules are evaluated in
  config order and the **first match wins**, so a broad rule placed early
  shadows later, more specific ones. `Config.category_for()` consults
  sub-rules first and falls back to the table, and it is the single
  classification entry point: the live watcher (`Organizer`), the one-shot
  `--once` pass, and `plan_actions()` all call it, so a file's category and
  rendered destination are identical whichever path inspects it — and the
  category is reported verbatim everywhere, never re-derived from the suffix
  via the table (`watch-diag`'s `matched_rules` would otherwise misreport a
  sub-routed file). The check is pure path/string work and never touches the
  filesystem. Malformed rules are rejected at load with `ConfigError` (exit
  code 2): a non-list `sub_rules`, or any entry missing a non-empty
  `pattern`/`category` or a non-empty `extensions` list of `.`-prefixed
  strings. `check` prints the resolved rule list, and the rules are carried
  in `Config.to_dict()` — so they appear in `check --json` and
  `watch-diag --json`, and in the dashboard's `/api/status` and `/api/rules`
  payloads. Opt-in: an absent key means the previous extension-only
  behavior.

- **`dupes` — report-only duplicate detection** (`fs_organizer/duplicates.py`,
  `fs-organizer CONFIG dupes`). Scans exactly the scope the watcher acts on
  (top level of each watch folder, plus subfolders when `recursive: true`) and
  reports groups of 2+ identical files by SHA-256, honoring the same ignore
  list and target-root loop guard the organizer applies — organized output is
  never a candidate. **Deliberately report-only: it never moves, deletes, or
  renames anything.** A visibility tool first, because a duplicate reporter
  that could destroy data would break the safe-file-operations invariant
  (worst case is "nothing happened"). Resource bounds keep it safe on huge
  trees: a size pre-filter skips files whose byte size is unique among the
  candidates (provably unique — their content is never read), hashing is
  streamed in 1 MiB chunks (constant memory), and `--max-files` (default
  2000), `--max-mb` (default 256), and `--min-size` (default 1 byte) cap the
  work. Hitting a cap sets `truncated: true` in the payload rather than
  silently reporting a partial picture as complete; groups found before the
  cap are still whole. `--json` emits the machine-readable form. The same
  report is exposed to the dashboard as a read-only `GET /api/dupes` (safe to
  serve unauthenticated on the loopback-only server precisely because it
  mutates nothing and only ever returns paths under the watch folders) and
  rendered as a "Duplicates" card, with the oldest copy in each group marked
  as the likely original and a truncation notice when a cap was hit.

### Fixed

- **Dashboard polled a full filesystem scan every 2 seconds** (flaw #45).
  `/api/status` embedded `plan_summary()`, which walks every watch folder
  (bounded at 2000 files), and the dashboard polls that endpoint every 2s.
  An idle dashboard therefore burned a continuous tree walk, violating the
  low-resource-daemon invariant. The plan payload was also never rendered by
  the page, so the scan bought nothing. The redundant `plan` key is removed
  from `/api/status`, which now returns config, counters, and pause state
  only; the pre-existing on-demand `/api/plan` endpoint is unchanged and
  remains the way to get the plan.

- **`resume()` could strand held paths permanently** (flaw #47).
  `Watcher.resume()` swapped the `_held` set and only *then* cleared the
  `_paused` flag, and the dispatch callback read `_paused` outside the lock.
  A debouncer callback landing in that window inserted its path into the fresh
  `_held` set, where no resume would find it — the file stayed in the watch
  folder indefinitely until the next resume or a shutdown. The flag is now
  read, set, and cleared under the same lock that swaps `_held`, so
  dispatch/resume ordering is well-defined.

- **A malformed `{date:...}` template produced a garbage folder name**
  (flaw #48). `DestinationTemplate.render()` used `part[end + 1:]` when no
  closing brace was found; with `end == -1` that is `part[0:]`, so the
  malformed token was emitted a second time. The pattern `{category}/{date:%Y/%m}`
  rendered as `Documents/2025-12{date:%Y/%m}`. The malformed remainder is now
  dropped and the safe `%Y-%m` default is used. Well-formed patterns are
  unchanged, and a bad format still never crashes a move.

- **Stored XSS in the dashboard status card** (flaw #49). `renderStatus()`
  interpolated `${v}` into `innerHTML` without escaping, so a config-supplied
  value such as a `target_root` containing `<img src=x onerror=…>` executed
  script in the dashboard — a direct violation of the "escape
  user-influenced strings" invariant. Status rows are now assembled as an
  escaped array, and sub-rule patterns/extensions/categories are escaped too.
  File, event, and duplicate-rendering escaping was audited and is intact.

- **`watch-diag` crashed with a `NameError`** (flaw #50). `_cmd_watch_diag`
  called `_watch_diag(...)`, but the import hoist in this release added
  `render_watch_diag` and `check_report` to the `diagnostics` import without
  `watch_diag` itself — so `fs-organizer CONFIG watch-diag` failed at runtime
  with `NameError: name '_watch_diag' is not defined`. `watch_diag` is now
  imported under its real name and the call site uses it. The existing
  coverage called `diagnostics.watch_diag` directly and so never exercised the
  CLI path; two regression tests now drive the actual subcommand (human and
  `--json` output).

### Changed

- `check`, `watch-diag`, and `dupes` receive the config `main()` already
  loaded and validated instead of parsing the config file a second time.
- Loop-local imports in `_one_shot` (`age_policy_allows`, `classify_with_ai`)
  and in the `dupes` command hoisted to module scope.

### Removed

- Unused `collections.defaultdict` import in `pool.py`.

### Testing

373 tests pass, plus `scripts/smoke_dryrun.py` and `scripts/smoke_ui.py`.
New regression coverage: flaw #47 (pause/resume held-path race), flaw #48
(malformed `{date:…}` token), flaw #49 (dashboard escaping), flaw #50 (the
`watch-diag` CLI subcommand, human and `--json`), plus dedicated
`tests/test_duplicates.py` (streaming hashes, size pre-filter, hashing-budget
and file-cap truncation, ignore/loop-guard scope, recursive scope, and a
report-only assertion that the scan never mutates) and
`tests/test_sub_rules.py` (parsing/validation, extension gating, first-match-
wins precedence, bare-vs-slash and Windows-backslash patterns, agreement
across the one-shot pass, `plan_actions`, and the live organizer, and the
`~`-expansion, including the narrow guard that keeps `~scan.pdf` a file-NAME
glob instead of a username).

[0.4.0]: https://github.com/CoderGamerUnknow/File-managing-system/releases/tag/v0.4.0

[0.3.1]: https://github.com/CoderGamerUnknow/File-managing-system/releases/tag/v0.3.1
