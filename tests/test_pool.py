"""Tests for the worker pool and event debouncer."""
import threading
import time
from pathlib import Path

from fs_organizer.pool import Debouncer, WorkerPool, count_pending


class TestWorkerPool:
    def test_executes_submitted_tasks(self):
        pool = WorkerPool(num_workers=2)
        done = threading.Event()
        pool.submit(done.set)
        assert done.wait(timeout=5.0)
        pool.shutdown(wait=True)

    def test_executes_with_args(self):
        pool = WorkerPool(num_workers=1)
        results = []
        done = threading.Event()

        def add(a, b):
            results.append(a + b)
            done.set()

        pool.submit(add, 2, 3)
        assert done.wait(timeout=5.0)
        assert results == [5]
        pool.shutdown(wait=True)

    def test_worker_survives_exception(self):
        pool = WorkerPool(num_workers=1)
        done = threading.Event()

        def boom():
            raise RuntimeError("boom")

        pool.submit(boom)
        pool.submit(done.set)  # must still run after the failure
        assert done.wait(timeout=5.0)
        pool.shutdown(wait=True)

    def test_shutdown_stops_workers(self):
        pool = WorkerPool(num_workers=1)
        pool.shutdown(wait=True)
        for t in pool._workers:
            assert not t.is_alive()


class TestDebouncer:
    def test_fires_only_after_quiet_period(self):
        fired = []
        d = Debouncer(delay_seconds=0.15, callback=fired.append, check_interval=0.05)
        p = Path("a.txt")
        d.schedule(p)
        time.sleep(0.07)
        assert fired == [], "must not fire while events keep arriving"
        d.drain(timeout=2.0)
        assert fired == [p]
        d.shutdown()

    def test_coalesces_burst_of_schedules(self):
        fired = []
        d = Debouncer(delay_seconds=0.12, callback=fired.append, check_interval=0.05)
        p = Path("a.txt")
        for _ in range(5):
            d.schedule(p)  # like an editor writing in several chunks
            time.sleep(0.03)
        d.drain(timeout=2.0)
        assert fired.count(p) == 1
        d.shutdown()

    def test_different_paths_fire_separately(self):
        fired = []
        d = Debouncer(delay_seconds=0.1, callback=fired.append, check_interval=0.05)
        d.schedule(Path("a.txt"))
        d.schedule(Path("b.txt"))
        d.drain(timeout=2.0)
        assert sorted(fired) == [Path("a.txt"), Path("b.txt")]
        d.shutdown()

    def test_callback_exception_does_not_kill_loop(self):
        calls = {"n": 0}

        def cb(path):
            calls["n"] += 1
            raise RuntimeError("cb failed")

        d = Debouncer(delay_seconds=0.1, callback=cb, check_interval=0.05)
        d.schedule(Path("x.txt"))
        d.drain(timeout=2.0)
        assert calls["n"] == 1  # exception swallowed, pending cleared
        d.schedule(Path("y.txt"))
        d.drain(timeout=2.0)
        assert calls["n"] == 2  # loop still alive after the failure
        d.shutdown()

    def test_count_pending_helper(self):
        d = Debouncer(delay_seconds=5.0, callback=lambda p: None)
        d.schedule(Path("a.txt"))
        d.schedule(Path("b.txt"))
        assert count_pending(d) == 2
        d.shutdown()

    def test_reschedule_extends_delay(self):
        fired = []
        d = Debouncer(delay_seconds=0.2, callback=fired.append, check_interval=0.05)
        p = Path("a.txt")
        d.schedule(p)
        time.sleep(0.12)
        d.schedule(p)  # refresh just before it would fire
        time.sleep(0.12)
        assert fired == [], "refresh must push the fire time back"
        d.drain(timeout=2.0)
        assert fired == [p]
        d.shutdown()
