"""Watchdog-based folder watcher tying all components together."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .ai import classify_with_ai
from .config import Config
from .mover import _is_inside, move_file
from .pool import Debouncer, WorkerPool
from .rules import is_ignored

logger = logging.getLogger("fs_organizer")


class Organizer:
    """Decides what to do with a single path and performs the move."""

    # Transiently locked files (WinError 32) are re-scheduled this many times
    # before giving up and leaving the file in place (windows-compatibility
    # skill: locks are transient — antivirus/sync scans release them).
    MAX_LOCK_RETRIES = 3

    def __init__(self, config: Config, activity=None) -> None:
        self.config = config
        # Optional UI activity feed (duck-typed: needs .add(kind, path, detail)).
        self.activity = activity
        self._retry_lock = threading.Lock()
        # path -> attempts used so far (only while a file is being retried).
        self._lock_retries: dict[Path, int] = {}
        # Set by Watcher after construction; re-arms the debouncer for a path
        # (used to retry locked files after their stability window).
        self.reschedule = None  # Callable[[Path], None]

    def handle(self, path: Path) -> None:
        if not path.is_file():
            with self._retry_lock:
                self._lock_retries.pop(path, None)  # file gone; drop retry state
            return
        # V3 quiet hours: outside the configured window the organizer simply
        # does not act. The file is not held — it stays in the watch folder
        # and the next event (or a scan) after the window opens picks it up.
        if not self.config.quiet_hours.allows():
            logger.debug("Quiet hours: leaving %s for later", path.name)
            return
        if is_ignored(path, self.config.effective_ignore_patterns()):
            return
        # Skip files that live inside the target root already (avoid loops).
        if self._inside_root(path):
            return

        # Sub-rules first (per-source-folder routing), then the global table.
        category = self.config.category_for(path)
        if category is None:
            if not self.config.ai.enabled or path.suffix.lower() not in self.config.ai.extensions:
                logger.debug("No rule and AI not applicable for %s", path.name)
                return
            if self.activity is not None and hasattr(self.activity, "record_ai_call"):
                self.activity.record_ai_call()
            category = classify_with_ai(path, self.config.ai)
            if category is None:
                logger.info("AI classification failed for %s; leaving in place", path.name)
                if self.activity is not None:
                    self.activity.add("error", path, "AI classification failed")
                return
        # Size must be read BEFORE the move: afterwards the source is gone.
        try:
            size_for_stats = path.stat().st_size
        except OSError:
            size_for_stats = None
        result = move_file(path, category, self.config)
        if result.moved and size_for_stats is not None and self.activity is not None \
                and hasattr(self.activity, "record_bytes"):
            self.activity.record_bytes(size_for_stats)
        self._report(result, path)
        if result.transient:
            self._retry_locked(path)
        else:
            # Any definitive outcome (moved, skipped, dry-run) ends the retry
            # cycle for this path.
            with self._retry_lock:
                self._lock_retries.pop(path, None)

    def _report(self, result, path: Path) -> None:
        """Feed the (optional) UI activity log with the move outcome."""
        if self.activity is None:
            return
        if result.moved:
            # The final name may be collision-suffixed ("a (1).txt"), so show
            # the destination's name, not the source path's.
            self.activity.add(
                "moved", path, f"-> {result.destination}",
                display_name=result.destination.name,
            )
        elif result.would_move:
            self.activity.add("dryrun", path, "would move (dry-run)")
        elif result.transient:
            self.activity.add("skipped", path, "temporarily locked; retrying")
        elif result.skipped:
            kind = "refused" if result.refused else "skipped"
            self.activity.add(kind, path, result.reason)

    def _retry_locked(self, path: Path) -> None:
        """Re-schedule a transiently locked file; give up after MAX_LOCK_RETRIES.

        An exhausted path is marked with a negative sentinel so later events
        don't silently start a fresh retry cycle (retry-storm guard); any
        definitive outcome clears it (see handle()).
        """
        with self._retry_lock:
            attempts = self._lock_retries.get(path, 0)
            if attempts < 0:
                return  # already exhausted; do not re-arm again
            if attempts >= self.MAX_LOCK_RETRIES:
                self._lock_retries[path] = -1  # exhausted sentinel
                exceeded = True
            else:
                self._lock_retries[path] = attempts + 1
                exceeded = False
        if exceeded:
            logger.warning(
                "Still locked after %d attempts; leaving %s in place",
                self.MAX_LOCK_RETRIES, path.name,
            )
            return
        if self.reschedule is None:
            logger.warning("File locked (WinError 32) and no rescheduler; leaving %s in place", path)
            return
        logger.info(
            "File locked; re-checking %s after stability window (attempt %d/%d)",
            path.name, attempts + 1, self.MAX_LOCK_RETRIES,
        )
        self.reschedule(path)

    def _inside_root(self, path: Path) -> bool:
        # Form-tolerant containment: resolve() can hand back Windows
        # extended-path form ('\\\\?\\C:\\...'), which raw relative_to misses.
        return self.config and _is_inside(path, self.config.resolved_target_root())


class _EventHandler(FileSystemEventHandler):
    def __init__(self, organizer: Organizer, debouncer: Debouncer,
                 watch_roots: list[Path]) -> None:
        super().__init__()
        self.organizer = organizer
        self.debouncer = debouncer
        # Watchdog reports events for BOTH sides of a move. Scheduling the
        # source side (a path the user deliberately moved out of a watched
        # folder) makes the organizer yank files from outside its scope, so
        # only destinations inside a watched folder are handled.
        self._watch_roots = [Path(root).resolve() for root in watch_roots]

    def _in_watch_roots(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return any(
            resolved == root or root in resolved.parents for root in self._watch_roots
        )

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            path = Path(event.src_path)
            if self._in_watch_roots(path):
                self.debouncer.schedule(path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            path = Path(event.dest_path)
            if self._in_watch_roots(path):
                self.debouncer.schedule(path)


class Watcher:
    """Owns the observer, worker pool, and debouncer; shuts them down cleanly.

    V2: supports pause/resume. While paused, debounce-fired paths are held
    (re-scheduled into the debouncer) instead of dispatched — the events are
    NOT dropped, just deferred, and pausing never disables the organizer's
    own guards (a held path that was deleted while paused is simply dropped
    by handle()'s is_file() check on resume). No extra threads: holding is
    one dict re-insert inside the existing debouncer tick.
    """

    def __init__(self, config: Config, num_workers: int = 2, activity=None) -> None:
        self.config = config
        self.organizer = Organizer(config, activity=activity)
        self.pool = WorkerPool(num_workers=num_workers)
        self._paused = threading.Event()
        self._pause_lock = threading.Lock()
        self._held: set[Path] = set()

        def _dispatch(path: Path) -> None:
            # The paused flag is read INSIDE _pause_lock, the same lock
            # resume() takes when it clears the flag and swaps _held. Reading
            # it outside the lock let a callback pass the check, block on the
            # lock while resume() swapped, then insert into the fresh _held
            # set — a path no resume would ever dispatch (flaw #47).
            with self._pause_lock:
                if self._paused.is_set():
                    self._held.add(path)
                    was_held = True
                else:
                    was_held = False
            if was_held:
                if activity is not None:
                    activity.add("info", path, "held (paused)")
                return
            self.pool.submit_unique(self._dispatch_key(path), self.organizer.handle, path)

        self.debouncer = Debouncer(
            delay_seconds=max(config.file_stable_seconds, 0.05),
            # Keyed submit: duplicate events for the same path that land while
            # a handle() for it is queued or running coalesce into one task
            # (plus at most one follow-up pass), instead of queueing two moves.
            callback=_dispatch,
        )
        # Locked-file retries go back through the debouncer: the stability
        # window doubles as retry backoff, and submit_unique() keeps retries
        # from piling up if events keep arriving for the same path.
        self.organizer.reschedule = self.debouncer.schedule
        self.handler = _EventHandler(
            self.organizer, self.debouncer, config.resolved_watch_folders()
        )
        self.observer = Observer()
        self.observer.name = "fs-organizer-observer"
        for folder in config.resolved_watch_folders():
            self.observer.schedule(self.handler, str(folder), recursive=config.recursive)
            logger.info("Watching %s", folder)
        self.observer.daemon = True

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        """Stop acting on files; arriving events are held until resume()."""
        # Set under the same lock resume() clears it with, so the
        # dispatch/resume ordering stays well-defined (flaw #47).
        with self._pause_lock:
            self._paused.set()
        if self.organizer.activity is not None and hasattr(self.organizer.activity, "set_paused"):
            self.organizer.activity.set_paused(True)
        logger.info("Paused: events are being held")

    def resume(self) -> None:
        """Dispatch everything held while paused (dedup via submit_unique).

        The paused flag is cleared INSIDE the same lock that swaps ``_held``.
        Clearing it after the swap left a window where a debouncer callback
        could observe "paused", take the lock, and insert into the *fresh*
        ``_held`` set — a path that no resume would ever dispatch, silently
        stranded until the next resume or shutdown (flaw #47).
        """
        with self._pause_lock:
            self._paused.clear()
            held, self._held = self._held, set()
        if self.organizer.activity is not None and hasattr(self.organizer.activity, "set_paused"):
            self.organizer.activity.set_paused(False)
        for path in held:
            self.pool.submit_unique(self._dispatch_key(path), self.organizer.handle, path)
        logger.info("Resumed: %d held path(s) dispatched", len(held))

    def drain_held(self, timeout: float = 5.0) -> None:
        """Resume-and-dispatch helper for shutdown: releases held paths even
        if still paused, so a paused daemon shuts down without losing them."""
        with self._pause_lock:
            has_held = bool(self._held)
        if has_held:
            self.resume()

    @staticmethod
    def _dispatch_key(path: Path) -> Path:
        """Dedupe key: the resolved path, so case/alias variants of one file
        map to the same key."""
        try:
            return path.resolve()
        except OSError:
            return path

    def start(self) -> None:
        self.observer.start()

    def apply_config(self, new_config: Config) -> None:
        """Hot-swap a new Config: re-schedule watches, keep threads and history.

        Called from the dashboard's reload action. The observer is stopped
        and restarted around the re-schedule (watchdog supports schedule/
        unschedule_all on a live observer; a full stop/start is simpler and
        bounded). Pending debounced work keeps running with the old config
        object — its decisions were made under it, and the guards behave
        identically. Raises on failure so the caller can revert; after a
        failed rebuild the old watches stay scheduled (best effort).
        """
        old_config = self.config
        old_watches = old_config.resolved_watch_folders()
        self.config = new_config
        self.observer.unschedule_all()
        try:
            for folder in new_config.resolved_watch_folders():
                self.observer.schedule(self.handler, str(folder), recursive=new_config.recursive)
                logger.info("Watching %s", folder)
            self.handler._watch_roots = [Path(root).resolve() for root in new_config.resolved_watch_folders()]
            self.organizer.config = new_config
        except Exception:
            # Full revert: the previous config object stays authoritative and
            # its watches are re-scheduled best effort so the daemon keeps
            # operating exactly as before the reload attempt.
            self.config = old_config
            self.organizer.config = old_config
            self.observer.unschedule_all()
            for folder in old_watches:
                try:
                    self.observer.schedule(self.handler, str(folder), recursive=old_config.recursive)
                except Exception:
                    logger.exception("Could not re-schedule %s during revert", folder)
            raise

    def stop(self, join_timeout: float = 3.0, drain_timeout: float = 5.0) -> None:
        """Stop watching, let pending debounced files be handled, then shut down.

        `drain_timeout` bounds how long we wait for paths that were scheduled
        shortly before shutdown to clear their grace period; workers then
        finish every already-queued move before their threads exit.
        """
        self.observer.stop()
        self.observer.join(timeout=join_timeout)
        if self.observer.is_alive():
            logger.warning("Observer did not stop within %.1fs", join_timeout)
        # A paused watcher must not strand held paths: release them so the
        # drain below processes them like any other pending work.
        self.drain_held()
        # Workers must still be running while the debouncer fires pending
        # paths — drain first, THEN tear the threads down.
        self.debouncer.drain(timeout=drain_timeout)
        self.debouncer.shutdown()
        self.pool.shutdown(wait=True, timeout=max(drain_timeout, 5.0))

