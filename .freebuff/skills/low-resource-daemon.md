# Skill: Low-Resource Daemon

fs-organizer runs at every login in the background (pythonw, no console).
Its resource budget is a hard requirement, not an optimization. Any change
must keep the idle footprint negligible.

## Rules

1. **Event-driven only.** watchdog (`Observer`) is the single source of
   events. Never add polling loops over the filesystem, directory scans on a
   timer, or per-file timers. The only allowed polling is:
   - `Debouncer._loop` — wakes on `Event.wait(check_interval=0.25s)` and does
     O(pending) dict work, nothing I/O.
   - `__main__._wait_forever` — 0.5 s event-wait slices (signal-interruptible
     on Windows; see windows-compatibility skill).
2. **Fixed small thread pool.** `WorkerPool(num_workers=2)` — do not scale
   workers with queue depth. File moves are I/O-bound; 2 threads saturate
   disk just fine and cap peak memory.
3. **Bounded queue.** The pool queue has `maxsize=1000`. On overflow, drop
   the task and log a warning — never grow unbounded. `submit_unique`
   additionally coalesces duplicate events per file so editor/save bursts
   cannot queue hundreds of identical tasks.
4. **Debounce before work.** All events pass through the `Debouncer`
   (`file_stable_seconds`, default 1.0 s) so a file is handled once, after
   it is quiet — not once per write. This is the main CPU saver.
5. **Daemon threads everywhere.** Worker, debouncer, and observer threads are
   daemon threads; `Watcher.stop()` stops and joins them (draining pending
   files first) so nothing lingers after exit.
6. **Capped logging.** File logging uses `RotatingFileHandler`
   (1 MB × 3 files max). Under pythonw with no `--log-file`, logging is
   disabled entirely. Debug level (`-v`) is opt-in only.
7. **Dependency budget.** Runtime dependencies: `watchdog`. The AI client is
   stdlib `urllib`. Do not add SDKs, background services, or IPC layers.
8. **Idle cost target.** With no filesystem activity: no timers beyond the
   debouncer slice, no network, no disk I/O. AI classification only runs for
   configured extensions with no matching rule — one bounded request per file.
