"""Concurrency primitives: a non-blocking worker pool and an event debouncer."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("fs_organizer")


class WorkerPool:
    """A fixed-size daemon thread pool executing callables from a queue.

    Besides plain `submit`, `submit_unique(key, ...)` coalesces duplicate
    tasks across threads: while a task for `key` is queued or running, new
    submits with the same key are absorbed. If any arrive while the task is
    actually running, exactly one follow-up run is scheduled on completion,
    so the newest state is always processed exactly once more.
    """

    def __init__(self, num_workers: int = 2, maxsize: int = 1000) -> None:
        # Bounded queue: an event flood (sync app, huge unzip) must never be
        # able to exhaust memory; overflow is dropped with a warning instead.
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=max(1, maxsize))
        self._workers: list[threading.Thread] = []
        self._shutdown = threading.Event()
        self._lock = threading.Lock()
        # Keys with a task queued OR currently executing.
        self._active: set = set()
        # Keys whose task a worker is executing right now (subset of _active).
        self._running: set = set()
        # key -> (func, args): a duplicate arrived while the task for that
        # key was running; re-run it once when the current run finishes.
        self._pending_rearm: dict = {}
        for i in range(max(1, num_workers)):
            t = threading.Thread(target=self._run, name=f"fs-organizer-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def _run(self) -> None:
        while True:
            try:
                func, args, key = self._queue.get(timeout=0.5)
            except queue.Empty:
                # Exit only once shutdown is requested AND the queue is
                # empty, so tasks queued before shutdown still run
                # (files that arrived just before Ctrl+C are not dropped).
                if self._shutdown.is_set():
                    break
                continue
            if key is not None:
                with self._lock:
                    self._running.add(key)
            try:
                func(*args)
            except Exception:  # noqa: BLE001 - workers must never die
                logger.exception("Worker task failed")
            finally:
                self._queue.task_done()
                self._finish(key)

    def _finish(self, key) -> None:
        """Release the task's key; honor any duplicate that arrived mid-run."""
        if key is None:
            return
        with self._lock:
            self._running.discard(key)
            self._active.discard(key)
            rearm = self._pending_rearm.pop(key, None)
            if rearm is not None:
                func, args = rearm
                try:
                    self._queue.put_nowait((func, args, key))
                    self._active.add(key)  # re-armed run is active again
                except queue.Full:
                    # Overflow policy: drop with a warning (bounded queue).
                    logger.warning(
                        "Work queue full; dropping re-armed task for %r", key
                    )

    def submit(self, func, *args) -> bool:
        """
        Queue a callable without blocking. Returns False (and logs a warning)
        when the bounded queue is full — the task is dropped so that an event
        flood can never exhaust memory.

        Plain submits are not deduped (key=None); use submit_unique for that.
        """
        try:
            self._queue.put_nowait((func, args, None))
            return True
        except queue.Full:
            logger.warning("Work queue full (maxsize=%d); dropping task", self._queue.maxsize)
            return False

    def submit_unique(self, key, func, *args) -> bool:
        """
        Queue `func(*args)` under `key` unless a task for that key is already
        queued or running — then coalesce instead (returns True; the work is
        covered by the in-flight task). If duplicates arrive while the task is
        *running*, exactly one follow-up run is scheduled on completion so the
        newest file state is processed once more.

        Returns False only when the bounded queue is full (task dropped).
        """
        with self._lock:
            if key in self._active:
                # Duplicate while queued/running: absorb it. Only a duplicate
                # arriving during *execution* needs a follow-up pass; one that
                # arrives while the task is merely queued is fully covered.
                if key in self._running:
                    self._pending_rearm[key] = (func, args)
                return True
            self._active.add(key)
            try:
                self._queue.put_nowait((func, args, key))
                return True
            except queue.Full:
                # Never leave the key stuck active with no task behind it.
                self._active.discard(key)
                logger.warning("Work queue full (maxsize=%d); dropping task", self._queue.maxsize)
                return False

    def shutdown(self, wait: bool = False, timeout: float | None = 2.0) -> None:
        """Request shutdown. Workers first finish every already-queued task,
        then exit; `wait` joins them (bounded by `timeout`)."""
        self._shutdown.set()
        if wait:
            for t in self._workers:
                t.join(timeout=timeout)


class Debouncer:
    """
    Collects filesystem events per path and only fires the callback once the
    path has been quiet for `delay_seconds` — so editors' multi-write saves
    and partial downloads don't trigger moves on half-written files.

    Callbacks run OUTSIDE the lock by design: a callback may safely re-enter
    schedule() (e.g. to reschedule a path) without deadlocking.
    """

    def __init__(self, delay_seconds: float, callback, check_interval: float = 0.25) -> None:
        self._delay = max(0.05, delay_seconds)
        self._callback = callback
        self._check_interval = max(0.05, check_interval)
        self._pending: dict[Path, float] = {}
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="fs-organizer-debouncer", daemon=True)
        self._thread.start()

    def schedule(self, path: Path) -> None:
        """Register/refresh a pending event for `path`."""
        with self._lock:
            self._pending[path] = time.monotonic() + self._delay

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            now = time.monotonic()
            ready: list[Path] = []
            with self._lock:
                for path, due in list(self._pending.items()):
                    if due <= now:
                        ready.append(path)
                        del self._pending[path]
            for path in ready:
                try:
                    self._callback(path)
                except Exception:  # noqa: BLE001
                    logger.exception("Debouncer callback failed for %s", path)
            self._shutdown.wait(self._check_interval)

    def drain(self, timeout: float = 5.0) -> None:
        """Block until all pending events have fired (for tests/shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return
            time.sleep(0.05)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._thread.join(timeout=2.0)


def count_pending(debouncer: Debouncer) -> int:
    """Test helper: number of currently pending debounced paths."""
    with debouncer._lock:
        return len(debouncer._pending)
