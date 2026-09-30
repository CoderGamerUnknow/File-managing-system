"""Regression tests for the flaws found in the multi-pass review.

Each test names the flaw it guards against. If one of these fails, a fix
has regressed — check the numbered comments in the module docstrings of
mover/watcher/pool/rules/config/ai/__main__/journal.
"""
import json
import logging
import shutil
import threading
import time
from pathlib import Path

import pytest

from fs_organizer.ai import classify_with_ai
from fs_organizer.config import AIConfig, Config, ConfigError, JournalConfig, load_config
from fs_organizer.mover import move_file, plan_actions
from fs_organizer.pool import Debouncer, WorkerPool
from fs_organizer.rules import is_ignored
from fs_organizer.watcher import Watcher, _EventHandler, Organizer

from helpers import make_config


@pytest.fixture
def clean_root_logger():
    """Snapshot root logging state; restore after tests that reconfigure it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_disable = root.manager.disable
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    logging.disable(saved_disable)


# ---------------------------------------------------------------- flaw #1
class TestFlaw1MoveRace:
    def test_concurrent_same_name_moves_both_survive(self, tmp_path, monkeypatch):
        """Two workers moving same-named files must not overwrite each other."""
        import fs_organizer.mover as mover

        desk, dl, out = tmp_path / "Desktop", tmp_path / "Downloads", tmp_path / "Organized"
        for d in (desk, dl):
            d.mkdir()
        cfg = Config(
            watch_folders=[str(desk), str(dl)],
            target_rules={".pdf": "Documents"},
            target_root=str(out),
        )
        (desk / "report.pdf").write_text("DESKTOP-VERSION")
        (dl / "report.pdf").write_text("DOWNLOADS-VERSION")

        started = threading.Event()
        release = threading.Event()
        real_move = mover.shutil.move

        def slow_move(src, dst):
            if Path(dst).name == "report.pdf":
                started.set()
                release.wait(timeout=10)  # hold the critical section open
            return real_move(src, dst)

        monkeypatch.setattr(mover.shutil, "move", slow_move)
        done = {}

        def run(key, src):
            done[key] = mover.move_file(src, "Documents", cfg)

        t1 = threading.Thread(target=run, args=("first", desk / "report.pdf"))
        t2 = threading.Thread(target=run, args=("second", dl / "report.pdf"))
        t1.start()
        assert started.wait(timeout=10), "first move never entered the critical section"
        t2.start()
        time.sleep(0.3)
        # While the first move is in-flight, the second must be blocked by the
        # per-directory lock — it must NOT have completed (the old bug did).
        assert "second" not in done, "second move ran while first was mid-move: race is back"
        release.set()
        t1.join(timeout=10)
        t2.join(timeout=10)

        names = sorted(p.name for p in (out / "Documents").iterdir())
        assert names == ["report (1).pdf", "report.pdf"], "a file was lost or duplicated"
        contents = {
            (out / "Documents" / "report.pdf").read_text(),
            (out / "Documents" / "report (1).pdf").read_text(),
        }
        assert contents == {"DESKTOP-VERSION", "DOWNLOADS-VERSION"}, "content was overwritten"

    def test_per_directory_locks_are_shared(self, tmp_path):
        import fs_organizer.mover as mover

        d1 = tmp_path / "a"
        d1s = mover._lock_for(d1)
        assert mover._lock_for(d1) is d1s  # same dir -> same lock
        assert mover._lock_for(tmp_path / "b") is not d1s  # different dir -> own lock


# ---------------------------------------------------------------- flaw #2
class TestFlaw2OneShotLoopGuard:
    def test_one_shot_never_reprocesses_target_root(self, tmp_path, capsys):
        """--once must skip files already inside the target root."""
        import json

        from fs_organizer.__main__ import _one_shot
        from fs_organizer.config import load_config

        watch = tmp_path / "watch"
        watch.mkdir()
        troot = watch / "Organized"  # target INSIDE the watch folder
        (troot / "Documents").mkdir(parents=True)
        organized = troot / "Documents" / "already.pdf"
        organized.write_text("x")

        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "target_rules": {".pdf": "Documents"},
            "target_root": str(troot),
            "ai": {"enabled": False},
        }), encoding="utf-8")

        rc = _one_shot(load_config(cfg))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Organized 0 file(s)" in out
        assert organized.exists()


# ---------------------------------------------------------------- flaw #3
class TestFlaw3RuntimeErrorEscape:
    def test_openai_without_key_returns_none(self, tmp_path, monkeypatch):
        """classify_with_ai must never raise — even for config errors."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="openai", api_key=None,
                       model="gpt-4", allowed_subfolders=["Documents"],
                       extensions=[".xyz"])
        assert classify_with_ai(f, cfg) is None  # used to raise RuntimeError


# ---------------------------------------------------------------- flaw #4
class TestFlaw4WatchFolderScope:
    def test_moved_file_outside_watch_roots_not_scheduled(self, tmp_path):
        """on_moved must ignore destinations outside the watched folders."""
        calls = []

        class FakeDebouncer:
            def schedule(self, path):
                calls.append(path)

        from watchdog.events import FileMovedEvent

        handler = _EventHandler(Organizer(make_config(tmp_path)), FakeDebouncer(),
                                [tmp_path / "watch"])
        outside = tmp_path / "Elsewhere" / "report.pdf"
        handler.on_moved(FileMovedEvent(str(tmp_path / "b.tmp"), str(outside)))
        assert calls == [], "organizer reached outside its watch folders"

    def test_moved_file_inside_watch_roots_is_scheduled(self, tmp_path):
        calls = []

        class FakeDebouncer:
            def schedule(self, path):
                calls.append(path)

        from watchdog.events import FileMovedEvent

        handler = _EventHandler(Organizer(make_config(tmp_path)), FakeDebouncer(),
                                [tmp_path / "watch"])
        inside = tmp_path / "watch" / "report.pdf"
        handler.on_moved(FileMovedEvent(str(tmp_path / "b.tmp"), str(inside)))
        assert calls == [inside]

    def test_created_file_outside_watch_roots_not_scheduled(self, tmp_path):
        calls = []

        class FakeDebouncer:
            def schedule(self, path):
                calls.append(path)

        from watchdog.events import FileCreatedEvent

        handler = _EventHandler(Organizer(make_config(tmp_path)), FakeDebouncer(),
                                [tmp_path / "watch"])
        handler.on_created(FileCreatedEvent(str(tmp_path / "Elsewhere" / "x.txt")))
        assert calls == []


# ---------------------------------------------------------------- flaw #5
class TestFlaw5AIAllowListValidation:
    def test_enabled_with_empty_allowed_subfolders_raises(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": {"enabled": True, "provider": "ollama", "allowed_subfolders": []},
        }), encoding="utf-8")
        with pytest.raises(ConfigError, match="allowed_subfolders"):
            load_config(cfg)

    def test_ai_must_be_an_object(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": [],
        }), encoding="utf-8")
        with pytest.raises(ConfigError, match="'ai' must be an object"):
            load_config(cfg)

    def test_ai_null_is_accepted(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": None,
        }), encoding="utf-8")
        loaded = load_config(cfg)
        assert loaded.ai.enabled is False


# ---------------------------------------------------------------- flaw #6
class TestFlaw6AICaseInsensitive:
    def test_lowercase_category_accepted(self, tmp_path, monkeypatch):
        from fs_organizer import ai as aim

        monkeypatch.setattr(
            aim, "_request_json",
            lambda *a, **k: {"message": {"content": '{"category": "documents"}'}},
        )
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="ollama",
                       allowed_subfolders=["Documents"], extensions=[".xyz"])
        assert classify_with_ai(f, cfg) == "Documents"  # canonical spelling

    def test_padded_category_accepted(self, tmp_path, monkeypatch):
        from fs_organizer import ai as aim

        monkeypatch.setattr(
            aim, "_request_json",
            lambda *a, **k: {"message": {"content": '{"category": "  Music  "}'}},
        )
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="ollama",
                       allowed_subfolders=["Music"], extensions=[".xyz"])
        assert classify_with_ai(f, cfg) == "Music"


# ---------------------------------------------------------------- flaw #7
class TestFlaw7LoggingReconfiguration:
    def test_configure_logging_can_be_called_twice(self, clean_root_logger):
        """Second _configure_logging call must actually apply (force=True)."""
        from fs_organizer.__main__ import _configure_logging

        _configure_logging(verbose=True, log_file=None)
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        n_after_first = len(root.handlers)

        _configure_logging(verbose=False, log_file=None)
        assert root.level == logging.INFO, "second call was a silent no-op"
        assert len(root.handlers) == n_after_first, "handlers duplicated"

    def test_no_stream_handler_when_stderr_missing(self, clean_root_logger, monkeypatch):
        """Under pythonw sys.stderr is None — no StreamHandler then."""
        import fs_organizer.__main__ as m

        monkeypatch.setattr(m.sys, "stderr", None)
        handlers = m._build_log_handlers(None)
        assert handlers == []

    def test_pythonw_without_log_file_disables_logging(self, clean_root_logger, monkeypatch):
        from fs_organizer.__main__ import _configure_logging
        import fs_organizer.__main__ as m

        monkeypatch.setattr(m.sys, "stderr", None)
        _configure_logging(verbose=False, log_file=None)
        assert logging.getLogger().disabled or logging.root.manager.disable >= logging.CRITICAL


# ---------------------------------------------------------------- flaw #8
class TestFlaw8DryRunCount:
    def test_dry_run_reports_would_move_count(self, tmp_path, capsys):
        import json

        from fs_organizer.__main__ import _one_shot
        from fs_organizer.config import load_config

        watch = tmp_path / "watch"
        watch.mkdir()
        (watch / "a.txt").write_text("x")
        (watch / "b.txt").write_text("y")
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "target_rules": {".txt": "Documents"},
            "target_root": str(tmp_path / "out"),
            "dry_run": True,
            "ai": {"enabled": False},
        }), encoding="utf-8")

        rc = _one_shot(load_config(cfg))
        out = capsys.readouterr().out
        assert rc == 0
        assert "would organize 2 file(s)" in out.lower(), (
            "dry-run must count would-be moves, not print 0"
        )
        assert (watch / "a.txt").exists()  # nothing actually moved


# ---------------------------------------------------------------- flaw #9
class TestFlaw9IgnorePatternScope:
    def test_name_pattern_never_matches_full_path(self):
        # Name chosen with no 'e' and not starting with 'c', so name matching
        # can't accidentally succeed — only full-path matching would.
        p = Path(r"C:\Users\sharm\Downloads\IMG_0001.png")
        assert is_ignored(p, ["c*"]) is False, "name glob matched the drive letter"
        assert is_ignored(p, ["*e*"]) is False, "name glob matched the whole path"

    def test_path_patterns_require_separator(self):
        p = Path(r"C:\Users\sharm\Downloads\setup.exe")
        assert is_ignored(p, ["**/Downloads/**"]) is True
        assert is_ignored(p, [r"**\Downloads\**"]) is True  # backslashes normalized
        assert is_ignored(p, ["Downloads/*"]) is False  # no leading **

    def test_name_patterns_still_work(self):
        p = Path(r"C:\Users\sharm\Downloads\song.mp3.tmp")
        assert is_ignored(p, ["*.tmp", "Thumbs.db"]) is True
        assert is_ignored(Path(r"C:\x\.gitignore"), [".*"]) is True


# --------------------------------------------------------------- flaw #10
class TestFlaw10GracefulShutdown:
    def test_watcher_stop_shuts_everything_down(self, tmp_path):
        cfg = make_config(tmp_path)
        watcher = Watcher(cfg)
        watcher.start()
        time.sleep(0.2)
        watcher.stop()
        assert not watcher.observer.is_alive()
        for t in watcher.pool._workers:
            assert not t.is_alive(), "worker thread leaked"
        assert not watcher.debouncer._thread.is_alive(), "debouncer thread leaked"

    def test_pool_shutdown_joins_workers(self):
        pool = WorkerPool(num_workers=2)
        pool.shutdown(wait=True, timeout=5.0)
        for t in pool._workers:
            assert not t.is_alive()


# --------------------------------------------------------------- flaw #11
class TestFlaw11BoundedQueue:
    def test_queue_full_drops_task(self):
        pool = WorkerPool(num_workers=1, maxsize=2)
        release = threading.Event()
        busy_started = threading.Event()

        def block():
            busy_started.set()
            release.wait(timeout=10)

        assert pool.submit(block) is True
        assert busy_started.wait(timeout=10)
        assert pool.submit(print, "q1") is True
        assert pool.submit(print, "q2") is True
        assert pool.submit(print, "q3") is False, "unbounded queue is back"
        release.set()
        pool.shutdown(wait=True, timeout=5.0)


# --------------------------------------------------------------- flaw #12
class TestFlaw12CategoryTraversal:
    def test_traversal_category_refused(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "evil.txt"
        src.write_text("malicious")
        outside = tmp_path / "Outside"

        result = move_file(src, "../../Outside", cfg)
        assert result.skipped is True and result.moved is False
        assert src.exists(), "file must stay put on refused category"
        assert not outside.exists(), "files escaped the target root"

    def test_dotdot_inside_root_still_ok(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "ok.txt"
        src.write_text("x")
        # A category with no traversal must keep working
        result = move_file(src, "Documents", cfg)
        assert result.moved is True


# --------------------------------------------------------------- flaw #13
class TestFlaw13DebouncerReentrantSchedule:
    def test_callback_may_reschedule_without_deadlock(self):
        """A callback that re-enters schedule() must not deadlock the loop."""
        calls = []
        d = Debouncer(delay_seconds=0.05, callback=calls.append, check_interval=0.05)
        p = Path("a.txt")

        def cb(path):
            calls.append(path)
            if len(calls) < 2:
                d.schedule(path)  # re-entrant schedule from inside callback

        d2 = Debouncer(delay_seconds=0.05, callback=cb, check_interval=0.05)
        d2.schedule(p)
        # drain() can return between the loop popping a path and the callback
        # re-scheduling it, so poll for the second fire instead.
        deadline = time.monotonic() + 3.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(calls) == 2, "re-entrant schedule never fired (deadlock?)"
        d.shutdown()
        d2.shutdown()


# --------------------------------------------------------------- flaw #14
class TestFlaw14NonNegativeCoercion:
    def test_zero_accepted(self):
        from fs_organizer.config import _coerce_non_negative_number

        assert _coerce_non_negative_number(0, "x") == 0.0
        assert _coerce_non_negative_number(0.0, "x") == 0.0

    def test_negative_rejected(self):
        from fs_organizer.config import _coerce_non_negative_number, ConfigError

        with pytest.raises(ConfigError):
            _coerce_non_negative_number(-1, "x")

    def test_zero_stability_in_config(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "file_stable_seconds": 0,
        }), encoding="utf-8")
        assert load_config(cfg).file_stable_seconds == 0.0


# --------------------------------------------------------------- flaw #15
class TestFlaw15InterruptibleWait:
    def test_wait_forever_exits_when_event_set(self):
        from fs_organizer.__main__ import _wait_forever

        stop = threading.Event()
        stop.set()
        start = time.monotonic()
        _wait_forever(stop)  # must return immediately, no signal.pause needed
        assert time.monotonic() - start < 1.0


# --------------------------------------------------------------- flaw #16
class TestFlaw16SameFileError:
    def test_same_file_error_reported_as_skipped(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")

        def raise_same(src_s, dst_s):
            raise shutil.SameFileError("same file")

        monkeypatch.setattr(shutil, "move", raise_same)
        result = move_file(src, "Documents", cfg)
        assert result.skipped is True and result.moved is False


# --------------------------------------------------------------- flaw #17
class TestFlaw17AINeverRaises:
    """classify_with_ai must never raise, whatever the model replies."""

    def test_null_category_value_returns_none(self, tmp_path, monkeypatch):
        from fs_organizer import ai as aim

        monkeypatch.setattr(
            aim, "_request_json",
            lambda *a, **k: {"message": {"content": '{"category": null}'}},
        )
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="ollama",
                       allowed_subfolders=["Documents"], extensions=[".xyz"])
        assert classify_with_ai(f, cfg) is None  # used to raise AttributeError

    def test_non_string_category_returns_none(self, tmp_path, monkeypatch):
        from fs_organizer import ai as aim

        monkeypatch.setattr(
            aim, "_request_json",
            lambda *a, **k: {"message": {"content": '{"category": 42}'}},
        )
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="ollama",
                       allowed_subfolders=["Documents"], extensions=[".xyz"])
        assert classify_with_ai(f, cfg) is None

    def test_non_string_base_url_returns_none(self, tmp_path, monkeypatch):
        from fs_organizer import ai as aim

        def fail(*a, **k):
            raise AssertionError("must fail before any request is built")

        monkeypatch.setattr(aim, "_request_json", fail)
        f = tmp_path / "f.xyz"
        f.write_text("data")
        cfg = AIConfig(enabled=True, provider="ollama", base_url=123,  # type: ignore[arg-type]
                       allowed_subfolders=["Documents"], extensions=[".xyz"])
        assert classify_with_ai(f, cfg) is None  # used to raise AttributeError


# --------------------------------------------------------------- flaw #18
class TestFlaw18AIStringValidation:
    def test_non_string_base_url_rejected(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": {"enabled": True, "provider": "ollama",
                   "base_url": 123, "allowed_subfolders": ["Documents"]},
        }), encoding="utf-8")
        with pytest.raises(ConfigError, match="ai.base_url"):
            load_config(cfg)

    def test_non_string_model_rejected(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": {"enabled": True, "provider": "ollama",
                   "model": ["llama"], "allowed_subfolders": ["Documents"]},
        }), encoding="utf-8")
        with pytest.raises(ConfigError, match="ai.model"):
            load_config(cfg)

    def test_non_string_api_key_rejected(self, tmp_path):
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": {"enabled": True, "provider": "openai", "api_key": 7},
        }), encoding="utf-8")
        with pytest.raises(ConfigError, match="ai.api_key"):
            load_config(cfg)

    def test_null_api_key_still_accepted(self, tmp_path):
        """"api_key": null" means unset — must keep working."""
        import json

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "ai": {"enabled": False, "provider": "openai", "api_key": None},
        }), encoding="utf-8")
        assert load_config(cfg).ai.api_key is None


# --------------------------------------------------------------- flaw #19
class TestFlaw19GracefulDrainOnShutdown:
    def test_pool_shutdown_finishes_queued_tasks(self):
        """Tasks queued before shutdown() must run, not be dropped."""
        pool = WorkerPool(num_workers=1, maxsize=10)
        release = threading.Event()
        busy_started = threading.Event()
        done = threading.Event()

        def busy():
            busy_started.set()
            release.wait(timeout=10)

        def late():
            done.set()

        assert pool.submit(busy) is True
        assert busy_started.wait(timeout=10)
        assert pool.submit(late) is True  # queued while the worker is busy
        release.set()  # unblock the worker so it can reach shutdown cleanly
        pool.shutdown(wait=True, timeout=10.0)
        assert done.wait(timeout=10), "queued task was dropped on shutdown"
        for t in pool._workers:
            assert not t.is_alive()

    def test_watcher_stop_handles_files_pending_before_stop(self, tmp_path):
        """A file scheduled shortly before stop() must still be organized."""
        cfg = make_config(tmp_path)
        cfg.file_stable_seconds = 0.2
        src = tmp_path / "watch" / "late.txt"
        src.write_text("x")

        watcher = Watcher(cfg)
        watcher.start()
        watcher.debouncer.schedule(src)  # event arrives just before shutdown
        watcher.stop(drain_timeout=5.0)

        assert not src.exists(), "file pending at shutdown drain was dropped"
        assert (tmp_path / "out" / "Documents" / "late.txt").exists()
        for t in watcher.pool._workers:
            assert not t.is_alive(), "worker thread leaked"
        assert not watcher.debouncer._thread.is_alive(), "debouncer thread leaked"


# --------------------------------------------------------------- flaw #20
class TestFlaw20DuplicateEventsCoalesce:
    """Duplicate events for one path must never queue two moves (flaw #20)."""

    def test_duplicate_while_running_gets_single_followup(self):
        """A duplicate arriving mid-run is absorbed and re-runs exactly once."""
        from fs_organizer.pool import WorkerPool

        pool = WorkerPool(num_workers=1, maxsize=10)
        release = threading.Event()
        running = threading.Event()
        runs = []

        def task():
            runs.append(1)
            running.set()
            release.wait(timeout=10)

        assert pool.submit_unique("k", task) is True
        assert running.wait(timeout=10)               # task executing now
        assert pool.submit_unique("k", task) is True  # duplicate mid-run
        assert pool._queue.qsize() == 0, "duplicate got its own queue slot"
        release.set()

        deadline = time.monotonic() + 5.0
        while len(runs) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)  # would expose a spurious third run
        assert runs == [1, 1], f"expected exactly two runs, got {len(runs)}"
        pool.shutdown(wait=True, timeout=5.0)

    def test_queued_duplicate_fully_absorbed_no_extra_run(self):
        """A duplicate landing while the task is merely queued adds no run."""
        from fs_organizer.pool import WorkerPool

        pool = WorkerPool(num_workers=2, maxsize=10)
        release = threading.Event()
        busy1, busy2 = threading.Event(), threading.Event()
        runs = []

        def blocker(evt):
            evt.set()
            release.wait(timeout=10)

        def task():
            runs.append(1)

        pool.submit(blocker, busy1)   # occupy worker 1
        assert busy1.wait(timeout=10)
        pool.submit(blocker, busy2)   # occupy worker 2
        assert busy2.wait(timeout=10)
        assert pool.submit_unique("k", task) is True  # truly queued now
        assert pool.submit_unique("k", task) is True  # absorbed while queued
        assert pool._queue.qsize() == 1, "duplicate got its own queue slot"
        release.set()

        deadline = time.monotonic() + 5.0
        while len(runs) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        assert runs == [1], f"queued duplicate caused extra run(s): {len(runs)}"
        pool.shutdown(wait=True, timeout=5.0)

    def test_queue_full_releases_key_no_leak(self):
        """On queue-full the key must be released, not stuck active forever."""
        from fs_organizer.pool import WorkerPool

        pool = WorkerPool(num_workers=1, maxsize=1)
        release = threading.Event()
        busy_started = threading.Event()

        def busy():
            busy_started.set()
            release.wait(timeout=10)

        assert pool.submit_unique("k", busy) is True
        assert busy_started.wait(timeout=10)                 # worker busy
        assert pool.submit_unique("k2", print, "x") is True  # fills queue slot
        assert pool.submit_unique("k3", print, "y") is False  # queue full
        assert "k3" not in pool._active, "failed submit left key active (leak)"
        release.set()
        pool.shutdown(wait=True, timeout=5.0)

    def test_watcher_duplicate_events_single_move(self, tmp_path):
        """End-to-end sanity: repeated schedules never double-move a file."""
        from fs_organizer.config import Config

        (tmp_path / "watch").mkdir()
        cfg = Config(
            watch_folders=[str(tmp_path / "watch")],
            target_rules={".txt": "Documents"},
            target_root=str(tmp_path / "out"),
            file_stable_seconds=0.1,
        )
        src = tmp_path / "watch" / "dup.txt"
        src.write_text("x", encoding="utf-8")

        watcher = Watcher(cfg)
        watcher.start()
        for _ in range(5):
            # Simulated watchdog burst: after the first fires, later ones land
            # while the handle task is queued/running, exercising the re-arm.
            watcher.debouncer.schedule(src)
            time.sleep(0.12)
        watcher.stop(drain_timeout=5.0)

        docs = tmp_path / "out" / "Documents"
        names = sorted(p.name for p in docs.iterdir())
        assert names == ["dup.txt"], f"duplicate moves happened: {names}"
        assert not src.exists()


# --------------------------------------------------------------- flaw #21
class TestFlaw21SignalInterruptibleMainLoop:
    """On Windows a console signal cannot interrupt an in-progress
    Event.wait — the main loop must poll in short slices so Ctrl+C
    (and CTRL_BREAK from scripts/smoke_dryrun.py, which e2e-guards this)
    takes effect immediately instead of up to an hour later."""

    def test_wait_forever_polls_in_short_slices(self):
        from fs_organizer.__main__ import _wait_forever

        class FakeEvent:
            def __init__(self):
                self.timeouts = []

            def wait(self, timeout=None):
                self.timeouts.append(timeout)
                return True  # stop after the first slice

        ev = FakeEvent()
        _wait_forever(ev)  # type: ignore[arg-type]
        assert ev.timeouts, "_wait_forever must wait on the event"
        assert max(ev.timeouts) <= 2.0, (
            f"main-loop wait slice too long to be signal-interruptible "
            f"on Windows: {ev.timeouts}"
        )


# --------------------------------------------------------------- flaw #22
class TestFlaw22TransientLockRetry:
    """WinError 32 (sharing violation) is transient: the file must be
    re-checked after the stability window (windows-compatibility skill),
    not abandoned on first contact with an antivirus scan. Retries are
    capped, and an exhausted path must not start a fresh cycle forever."""

    def _locked_exc(self):
        exc = PermissionError(32, "The process cannot access the file")
        exc.winerror = 32  # errno 13 on POSIX; winerror carries the real code
        return exc

    def test_winerror32_classified_transient(self, tmp_path, monkeypatch):
        """PermissionError with winerror 32 -> transient, not permanent skip."""
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")

        def locked_move(src_s, dst_s):
            raise self._locked_exc()

        monkeypatch.setattr(mover.shutil, "move", locked_move)
        result = move_file(src, "Documents", cfg)
        assert result.transient is True, "WinError 32 must be flagged transient"
        assert result.skipped is True
        assert src.exists()

    def test_other_permission_error_not_transient(self, tmp_path, monkeypatch):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")

        def denied(src_s, dst_s):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(mover.shutil, "move", denied)
        result = move_file(src, "Documents", cfg)
        assert result.transient is False and result.skipped is True
        assert src.exists()

    def test_locked_file_retried_then_moves(self, tmp_path, monkeypatch):
        """A locked file is re-scheduled (bounded) and moves once unlocked."""
        from fs_organizer.mover import MoveResult
        import fs_organizer.watcher as watcher_mod

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")
        state = {"n": 0}

        def flaky_move(src_p, category, config):
            state["n"] += 1
            if state["n"] <= 2:
                # Real move_file catches the WinError-32 PermissionError and
                # reports it as transient; it never raises to the watcher.
                return MoveResult(skipped=True, transient=True)
            return MoveResult(moved=True)

        monkeypatch.setattr(watcher_mod, "move_file", flaky_move)
        org = watcher_mod.Organizer(cfg)
        scheduled = []
        org.reschedule = scheduled.append

        org.handle(src)          # attempt 1: locked -> retry scheduled
        org.handle(src)          # attempt 2: locked again -> retry scheduled
        assert state["n"] == 2
        assert len(scheduled) == 2
        org.handle(src)          # attempt 3: succeeds
        assert state["n"] == 3
        # Success clears retry state.
        assert src not in org._lock_retries
        org.handle(src)
        assert state["n"] == 4  # one more real attempt, no retry bookkeeping left

    def test_locked_file_gives_up_after_cap(self, tmp_path, monkeypatch):
        """After MAX_LOCK_RETRIES the file is left in place and the path is
        not re-armed forever (no retry storm from repeated events)."""
        from fs_organizer.mover import MoveResult
        import fs_organizer.watcher as watcher_mod

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")
        state = {"n": 0}

        def always_locked(src_p, category, config):
            state["n"] += 1
            return MoveResult(skipped=True, transient=True)

        monkeypatch.setattr(watcher_mod, "move_file", always_locked)
        org = watcher_mod.Organizer(cfg)
        scheduled = []
        org.reschedule = scheduled.append

        for _ in range(6):
            org.handle(src)
        assert len(scheduled) == watcher_mod.Organizer.MAX_LOCK_RETRIES, (
            f"expected exactly {watcher_mod.Organizer.MAX_LOCK_RETRIES} "
            f"re-arms, got {len(scheduled)}"
        )
        assert src.exists()
        # Exhausted: further locked attempts must not re-arm again.
        before = len(scheduled)
        org.handle(src)
        assert len(scheduled) == before, "exhausted path started a fresh retry cycle"

    def test_success_clears_exhausted_state(self, tmp_path, monkeypatch):
        """A definitive outcome (move ok) must clear exhausted/sentinel state."""
        from fs_organizer.mover import MoveResult
        import fs_organizer.watcher as watcher_mod

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")
        results = []

        def scripted(src_p, category, config):
            return results.pop(0)

        monkeypatch.setattr(watcher_mod, "move_file", scripted)
        org = watcher_mod.Organizer(cfg)
        scheduled = []
        org.reschedule = scheduled.append

        # Drive to exhaustion.
        for _ in range(watcher_mod.Organizer.MAX_LOCK_RETRIES + 1):
            results.append(MoveResult(skipped=True, transient=True))
            org.handle(src)
        exhausted_before = dict(org._lock_retries)
        assert any(v < 0 for v in exhausted_before.values()), "sentinel missing"

        # Now the move succeeds: state must be cleared.
        results.append(MoveResult(moved=True))
        org.handle(src)
        assert src not in org._lock_retries, "success did not clear retry state"

        # And a later lock can start a fresh cycle again.
        results.append(MoveResult(skipped=True, transient=True))
        org.handle(src)
        assert len(scheduled) >= 1


# --------------------------------------------------------------- flaw #23
class TestFlaw23SingleConfigDefinition:
    """Config and from_dict must each exist exactly once in config.py
    (a duplicated definition silently shadowed the first and broke the
    'ai.*' error contract)."""

    def test_single_definition_of_public_names(self):
        import inspect

        import fs_organizer.config as cfg

        for name in ("Config", "from_dict"):
            matches = [
                n for n, obj in vars(cfg).items()
                if n == name and inspect.getmodule(obj) is cfg
            ]
            assert matches == [name], f"{name} is defined more than once"


# --------------------------------------------------------------- flaw #24
class TestFlaw24EffectiveIgnoreEverywhere:
    """Mover, watcher, one-shot and diagnostics must all apply the same
    effective ignore list (built-ins + user patterns). A '*.part' file with
    a user ignore list must stay put, everywhere."""

    def _cfg(self, tmp_path) -> Config:
        return make_config(tmp_path, ignore_patterns=["*.bak"])

    def test_move_file_ignores_builtin_patterns(self, tmp_path):
        cfg = self._cfg(tmp_path)
        f = tmp_path / "watch" / "movie.part"
        f.write_text("x")
        result = move_file(f, "Videos", cfg)
        assert result.skipped and result.moved is False
        assert f.exists(), "a partial download was moved — built-in ignores not applied"

    def test_organizer_ignores_builtin_patterns(self, tmp_path):
        cfg = self._cfg(tmp_path)
        f = tmp_path / "watch" / "movie.part"
        f.write_text("x")
        Organizer(cfg).handle(f)
        assert f.exists(), "watcher moved a '*.part' file"

    def test_plan_actions_ignores_builtin_patterns(self, tmp_path):
        cfg = self._cfg(tmp_path)
        f = tmp_path / "watch" / "movie.part"
        f.write_text("x")
        data = plan_actions(cfg)
        assert data["counts"]["would_skip_pattern"] == 1
        assert data["counts"]["would_organize"] == 0

    def test_one_shot_ignores_builtin_patterns(self, tmp_path, capsys, monkeypatch):
        from fs_organizer.__main__ import _one_shot
        from fs_organizer.config import load_config

        watch = tmp_path / "watch"
        watch.mkdir()
        (watch / "movie.part").write_text("x")
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "target_rules": {".part": "Videos"},
            "target_root": str(tmp_path / "out"),
            "ignore_patterns": ["*.bak"],
            "ai": {"enabled": False},
        }))
        moved = _one_shot(load_config(cfg))
        assert moved == 0
        assert (watch / "movie.part").exists()


# --------------------------------------------------------------- flaw #25
class TestFlaw25ScanScopeMatchesWatcher:
    """Plans and diagnostics must scan the same scope the watcher organizes
    (top level of each watch folder)."""

    def test_nested_file_not_advertised(self, tmp_path):
        cfg = make_config(tmp_path)
        nested = tmp_path / "watch" / "sub" / "deep.txt"
        nested.parent.mkdir()
        nested.write_text("x")
        data = plan_actions(cfg)
        assert data["counts"]["would_organize"] == 0, (
            "plan advertises a nested file the watcher never schedules"
        )

    def test_scan_returns_never_exceeds_cap(self, tmp_path):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        for i in range(5):
            (tmp_path / "watch" / f"f{i}.txt").write_text("x")
        entries, truncated = mover._scan_files(cfg, 3)
        assert len(entries) <= 3
        assert truncated is True


# --------------------------------------------------------------- flaw #26
class TestFlaw26MismatchedCategories:
    """mismatched_categories is the symmetric difference between rule
    categories and AI categories — the old intersection-minus-union was
    identically empty and could never warn."""

    def test_symmetric_difference_reported(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.target_rules = {".txt": "Documents"}
        cfg.ai.allowed_subfolders = ["Notes", "Images"]
        cov = cfg.coverage_report()
        assert set(cov["mismatched_categories"]) == {"Documents", "Notes", "Images"}

    def test_matching_policy_reports_empty(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.target_rules = {".txt": "Documents"}
        cfg.ai.allowed_subfolders = ["Documents"]
        assert cfg.coverage_report()["mismatched_categories"] == []

    def test_no_provider_named_category(self, tmp_path):
        """AI-able extensions must not invent a fake category named after the
        provider in the coverage report."""
        cfg = make_config(tmp_path)
        cfg.ai.extensions = [".dat"]
        cov = cfg.coverage_report()
        assert cov["categories"] == {"Documents": [".txt"]}
        assert cov["uncovered"] == [".dat"]


# --------------------------------------------------------------- flaw #27
class TestFlaw27WatchDiagPerFolder:
    """watch-diag must report per-folder counts, not global totals repeated
    for every folder."""

    def test_counts_are_per_folder(self, tmp_path):
        import json

        from fs_organizer.diagnostics import watch_diag as _watch_diag

        w1, w2 = tmp_path / "w1", tmp_path / "w2"
        w1.mkdir()
        w2.mkdir()
        (w1 / "a.txt").write_text("x")
        (w1 / "b.txt").write_text("x")
        (w2 / "c.txt").write_text("x")
        cfg = Config(
            watch_folders=[str(w1), str(w2)],
            target_rules={".txt": "Documents"},
            target_root=str(tmp_path / "out"),
        )
        data = _watch_diag(cfg, [])
        assert data["files"][str(w1)]["would_organize"] == 2
        assert data["files"][str(w2)]["would_organize"] == 1

    def test_truncation_flagged_not_silent(self, tmp_path):
        """A folder with more files than the scan cap is reported with
        truncated=True (never silently capped)."""
        from fs_organizer.diagnostics import watch_diag as _watch_diag

        big = tmp_path / "big"
        big.mkdir()
        for i in range(2001):
            (big / f"f{i:04}.txt").write_text("x")
        cfg = Config(
            watch_folders=[str(big)],
            target_rules={".txt": "Documents"},
            target_root=str(tmp_path / "out"),
        )
        info = _watch_diag(cfg, [])["files"][str(big)]
        assert info["truncated"] is True
        assert info["total_files"] == 2000  # capped list, honestly labeled


# --------------------------------------------------------------- flaw #28
class TestFlaw28NoSecretsInDumps:
    """check --json and watch-diag --json must never print the API key."""

    def test_to_dict_redacts_api_key(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.ai = AIConfig(enabled=True, provider="openai", api_key="sk-secret",
                          allowed_subfolders=["Documents"])
        assert cfg.to_dict()["ai"]["api_key"] == "***"
        assert "sk-secret" not in json.dumps(cfg.to_dict(), default=str)

    def test_watch_diag_output_has_no_secret(self, tmp_path):
        from fs_organizer.diagnostics import watch_diag as _watch_diag

        cfg = make_config(tmp_path)
        cfg.ai = AIConfig(enabled=True, provider="openai", api_key="sk-secret",
                          allowed_subfolders=["Documents"])
        dumped = json.dumps(_watch_diag(cfg, []), default=str)
        assert "sk-secret" not in dumped


# --------------------------------------------------------------- flaw #29
class TestFlaw29ScanCountBaseline:
    """The dashboard's manual-scan counter must count only moves made by
    that scan, not every move since startup."""

    def test_scan_counts_only_new_moves(self, tmp_path):
        import fs_organizer.ui as ui_mod

        cfg = make_config(tmp_path)
        activity = ui_mod.ActivityLog()
        activity.add("moved", tmp_path / "watch" / "old.txt")  # pre-scan history

        (tmp_path / "watch" / "real.txt").write_text("x")
        state = ui_mod.DashboardState(cfg, activity)
        started, _ = state.run_once_async("scan")
        assert started
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and state.oneshot_running.is_set():
            time.sleep(0.05)
        assert state.oneshot_result["status"] == "done"
        assert state.oneshot_result["moved"] == 1, (
            "scan counter must equal the scan's own moves: not the historical "
            "event (2) and not a ring-buffer-capped approximation"
        )

    def test_scan_counter_not_capped_by_ring_buffer(self, tmp_path):
        """A scan moving more files than the ring holds must still report
        the exact count (the old event-counting approach reported 500)."""
        import fs_organizer.ui as ui_mod

        cfg = make_config(tmp_path)
        for i in range(700):
            (tmp_path / "watch" / f"f{i:03}.txt").write_text("x")
        state = ui_mod.DashboardState(cfg, ui_mod.ActivityLog())
        state.run_once_async("scan")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and state.oneshot_running.is_set():
            time.sleep(0.05)
        assert state.oneshot_result["status"] == "done"
        assert state.oneshot_result["moved"] == 700


# --------------------------------------------------------------- flaw #30
class TestFlaw30CompoundSuffixCollisions:
    def test_tar_gz_collision_suffixes_whole_archive(self, tmp_path):
        cfg = make_config(tmp_path)
        out = tmp_path / "out" / "Archives"
        out.mkdir(parents=True)
        (out / "bundle.tar.gz").write_text("old")
        src = tmp_path / "watch" / "bundle.tar.gz"
        src.write_text("new")
        result = move_file(src, "Archives", cfg)
        assert result.destination == out / "bundle (1).tar.gz"


# --------------------------------------------------------------- flaw #31
class TestFlaw31AIErrorPrefixes:
    """AI field errors must name the offending key ('ai.base_url', not
    'base_url') so users can find it in their config."""

    def test_non_string_base_url_message_names_ai_key(self, tmp_path):
        from fs_organizer.config import ConfigError

        (tmp_path / "watch").mkdir()
        with pytest.raises(ConfigError, match="ai.base_url"):
            from fs_organizer.config import from_dict as _from_dict

            _from_dict({
                "watch_folders": [str(tmp_path / "watch")],
                "ai": {"enabled": True, "provider": "ollama", "base_url": 123,
                       "allowed_subfolders": ["Documents"]},
            })


# --------------------------------------------------------------- flaw #32
class TestFlaw32ResolveFormStableGuard:
    """Path.resolve() intermittently returns Windows extended-path form
    ('\\\\?\\C:\\...'); the traversal guard must not let that make it refuse
    destinations that are in fact inside the target root."""

    def test_guard_ignores_extended_path_prefix(self, tmp_path):
        import fs_organizer.mover as mover

        root = tmp_path / "out"
        assert mover._is_inside(root / "Documents", root) is True
        assert mover._is_inside(Path("\\\\?\\" + str(root / "Documents")), root) is True, (
            "\\\\?\\-prefixed destination was treated as outside the root"
        )
        assert mover._is_inside((root / ".." / "Outside").resolve(), root) is False

    def test_guard_strips_repeated_extended_prefix(self, tmp_path):
        """The normalizer must strip repeated \\\\?\\ prefixes (idempotent).

        Constructed via a str-stub because Path() itself mangles the literal
        prefix; real extended paths come from resolve() and WinAPI calls.
        """
        import fs_organizer.mover as mover

        class LiteralPath:
            def __init__(self, s):
                self._s = s

            def __str__(self):
                return self._s

        root = tmp_path / "out"
        doubled = LiteralPath("\\\\?\\\\\\?\\" + str(root / "Documents"))
        assert mover._is_inside(doubled, root) is True
        single = LiteralPath("\\\\?\\" + str(root / "Documents"))
        assert mover._is_inside(single, root) is True

    def test_move_with_extended_form_resolved_dest_succeeds(self, tmp_path, monkeypatch):
        """Even when resolve() hands back an extended-path dest, the move happens."""
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x")
        real_resolve = Path.resolve

        def flaky_resolve(self, strict=False):
            out = real_resolve(self, strict=strict)
            if self.name == "Documents":
                return Path("\\\\?\\" + str(out))
            return out

        monkeypatch.setattr(Path, "resolve", flaky_resolve)
        result = move_file(src, "Documents", cfg)
        assert result.moved is True, (
            f"move refused: {result!r} — extended-path resolve broke the guard"
        )
        assert (tmp_path / "out" / "Documents" / "a.txt").exists()


# --------------------------------------------------------------- flaw #33
class TestFlaw33FormTolerantLoopGuards:
    """Every containment guard (one-shot loop guard, scan target-root guard,
    watcher organizer guard) must tolerate the \\\\?\\ extended-path form that
    resolve() intermittently produces — not just move_file's guard."""

    def test_one_shot_loop_guard_tolerant(self, tmp_path):
        from fs_organizer.__main__ import _inside_target_root

        cfg = make_config(tmp_path)
        ext_root = Path("\\\\?\\" + str(cfg.resolved_target_root()))
        inside = tmp_path / "out" / "Documents" / "a.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("x")

        class ExtCfg:
            def __getattr__(self, name):
                return getattr(cfg, name)

            def resolved_target_root(self):
                return ext_root

        assert _inside_target_root(inside, ExtCfg()) is True

    def test_scan_target_root_guard_tolerant(self, tmp_path):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        inside = tmp_path / "out" / "Documents" / "a.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("x")

        class ExtCfg:
            def __getattr__(self, name):
                return getattr(cfg, name)

            def resolved_target_root(self):
                return Path("\\\\?\\" + str(cfg.resolved_target_root()))

        entries, _ = mover._scan_files(ExtCfg(), 100)
        assert [e["decision"] for e in entries] == [], (
            "organized output inside an extended-form root was treated as input"
        )

    def test_organizer_guard_tolerant(self, tmp_path):
        cfg = make_config(tmp_path)
        inside = tmp_path / "out" / "Documents" / "a.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("x")

        class ExtCfg:
            def __getattr__(self, name):
                return getattr(cfg, name)

            def resolved_target_root(self):
                return Path("\\\\?\\" + str(cfg.resolved_target_root()))

        org = Organizer(ExtCfg())
        assert org._inside_root(inside) is True


# --------------------------------------------------------------- flaw #34
class TestFlaw34FilesPayloadTruncation:
    def test_exact_cap_is_not_truncation(self, tmp_path):
        from fs_organizer.views import files_payload

        cfg = make_config(tmp_path)
        (tmp_path / "out").mkdir()
        for i in range(5):
            (tmp_path / "out" / f"f{i}.txt").write_text("x")
        data = files_payload(cfg, max_files=5)
        assert data["total"] == 5 and data["truncated"] is False

    def test_over_cap_is_truncation(self, tmp_path):
        from fs_organizer.views import files_payload

        cfg = make_config(tmp_path)
        (tmp_path / "out").mkdir()
        for i in range(6):
            (tmp_path / "out" / f"f{i}.txt").write_text("x")
        data = files_payload(cfg, max_files=5)
        assert data["total"] == 5 and data["truncated"] is True


# --------------------------------------------------------------- flaw #35
class TestFlaw35VanishingFolderScan:
    def test_one_shot_survives_folder_vanishing_mid_scan(self, tmp_path, capsys):
        import fs_organizer.__main__ as m

        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        (watch / "a.txt").write_text("x")

        # Patch the filesystem primitive beneath _list_children: iterdir
        # raising OSError is what a vanished folder does mid-scan.
        real_iterdir = Path.iterdir

        def vanishing(self):
            if self.name == "watch":
                raise OSError(2, "folder vanished")
            return real_iterdir(self)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "iterdir", vanishing)
            moved = m._one_shot(cfg)
            assert moved == 0, "vanished folder must not crash --once"
            assert "Organized 0 file(s)" in capsys.readouterr().out
        finally:
            monkeypatch.undo()

    def test_watch_diag_matched_rules_survives_vanishing_folder(self, tmp_path):
        from fs_organizer.diagnostics import watch_diag

        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        (watch / "a.txt").write_text("x")

        real_iterdir = Path.iterdir

        def vanishing(self):
            if self.name == "watch":
                raise OSError(2, "folder vanished")
            return real_iterdir(self)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(Path, "iterdir", vanishing)
            data = watch_diag(cfg, [])
            assert data["files"][str(watch)]["matched_rules"] == []
        finally:
            monkeypatch.undo()


# ---------------------------------------------------------------- flaw #36
class TestFlaw36MoveJournal:
    """The JSONL move journal: audit trail + creation-date source."""

    def test_append_and_read_roundtrip(self, tmp_path):
        from fs_organizer import journal

        cfg = make_config(tmp_path)
        cfg.journal = JournalConfig(enabled=True, path=str(tmp_path / "j" / "moves.jsonl"))
        journal.append_move(
            cfg,
            tmp_path / "watch" / "a.txt",
            tmp_path / "out" / "Documents" / "a.txt",
            42,
        )
        entries = journal.read_entries(cfg.journal.resolved_path())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["src"].endswith("a.txt")
        assert entry["dest"].endswith("a.txt")
        assert entry["category"] == "Documents"
        assert entry["size"] == 42
        assert isinstance(entry["ts"], float)

    def test_disabled_journal_is_a_noop(self, tmp_path):
        from fs_organizer import journal

        cfg = make_config(tmp_path)  # journal defaults to disabled
        journal.append_move(cfg, tmp_path / "a.txt", tmp_path / "b.txt", 1)
        assert not journal.DEFAULT_JOURNAL_PATH().exists()

    def test_torn_final_line_is_skipped(self, tmp_path):
        """A crash mid-append leaves a partial JSON line; reading must survive."""
        from fs_organizer import journal

        path = tmp_path / "j.jsonl"
        good = json.dumps(
            {"ts": 2.0, "src": "s", "dest": "d", "category": "Documents", "size": 1}
        )
        path.write_text(good + "\n" + '{"ts": 3.0, "src": "to', encoding="utf-8")
        entries = journal.read_entries(path)
        assert [e["ts"] for e in entries] == [2.0]

    def test_since_ts_cursor(self, tmp_path):
        from fs_organizer import journal

        path = tmp_path / "j.jsonl"
        for ts in (1.0, 2.0, 3.0):
            path.open("a", encoding="utf-8").write(
                json.dumps({"ts": ts, "src": "s", "dest": "d", "category": "C", "size": 0})
                + "\n"
            )
        assert [e["ts"] for e in journal.read_entries(path, since_ts=2.0)] == [3.0]
        assert [e["ts"] for e in journal.read_entries(path)] == [1.0, 2.0, 3.0]

    def test_missing_journal_reads_as_empty(self, tmp_path):
        from fs_organizer import journal

        assert journal.read_entries(tmp_path / "nope.jsonl") == []

    def test_move_file_records_journal_entry(self, tmp_path):
        from fs_organizer import journal

        cfg = make_config(tmp_path)
        cfg.journal = JournalConfig(enabled=True, path=str(tmp_path / "j.jsonl"))
        src = tmp_path / "watch" / "a.txt"
        src.write_text("xyz", encoding="utf-8")
        result = move_file(src, "Documents", cfg)
        assert result.moved
        entries = journal.read_entries(cfg.journal.resolved_path())
        assert len(entries) == 1
        assert entries[0]["src"].endswith("watch\\a.txt") or entries[0]["src"].endswith("watch/a.txt")
        assert entries[0]["dest"].replace("\\", "/").endswith("out/Documents/a.txt")
        assert entries[0]["size"] == 3
        assert not src.exists()

    def test_unwritable_journal_never_breaks_moving(self, tmp_path):
        """Journaling is best-effort: an unwritable path must not fail a move."""
        blocker = tmp_path / "blocked"
        blocker.write_text("this is a file, not a directory")
        cfg = make_config(tmp_path)
        cfg.journal = JournalConfig(enabled=True, path=str(blocker / "j.jsonl"))
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        result = move_file(src, "Documents", cfg)
        assert result.moved and not src.exists()

    def test_files_payload_prefers_journal_creation_date(self, tmp_path):
        """On birthtime-less filesystems the mtime IS the move time; the
        journal's ts must win so the date isn't mislabeled forever."""
        import os as _os

        from fs_organizer import journal
        from fs_organizer.views import files_payload

        cfg = make_config(tmp_path)
        cfg.journal = JournalConfig(enabled=True, path=str(tmp_path / "j.jsonl"))
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        move_file(src, "Documents", cfg)
        dest = tmp_path / "out" / "Documents" / "a.txt"
        wrong = time.time() - 100_000  # a mtime that would be a wrong "creation date"
        _os.utime(dest, (wrong, wrong))

        journal_ts = journal.read_entries(cfg.journal.resolved_path())[0]["ts"]
        data = files_payload(cfg)
        rows = [r for rows in data["groups"].values() for r in rows]
        row = next(r for r in rows if r["name"] == "a.txt")
        assert abs(row["created"] - journal_ts) < 5  # journal wins
        assert row["created"] != int(wrong)

    def test_files_payload_without_journal_uses_stat_fallback(self, tmp_path):
        import os as _os

        from fs_organizer.views import files_payload

        cfg = make_config(tmp_path)  # journal disabled
        dest = tmp_path / "out" / "Documents"
        dest.mkdir(parents=True)
        f = dest / "old.txt"
        f.write_text("x", encoding="utf-8")
        ts = time.time() - 50_000
        _os.utime(f, (ts, ts))
        data = files_payload(cfg)
        rows = [r for rows in data["groups"].values() for r in rows]
        row = next(r for r in rows if r["name"] == "old.txt")
        stat = f.stat()
        expected = getattr(stat, "st_birthtime", None) or stat.st_mtime
        assert row["created"] == int(expected)  # unchanged legacy behavior


# ---------------------------------------------------------------- flaw #37
class TestFlaw37StagedCrossVolumeMove:
    """copy -> fsync -> rename -> delete so a crash can't duplicate data."""

    def test_cross_volume_decision_uses_splitdrive(self, tmp_path, monkeypatch):
        import fs_organizer.mover as mover

        assert mover._same_volume(Path("C:/a"), Path("C:/b"))
        assert not mover._same_volume(Path("C:/a"), Path("D:/b"))
        assert mover._same_volume(Path("\\\\srv\\share\\a"), Path("\\\\srv\\share\\b"))

    def test_same_volume_move_keeps_shutil_path(self, tmp_path, monkeypatch):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("DATA", encoding="utf-8")
        calls = []
        real_move = mover.shutil.move
        monkeypatch.setattr(mover, "_staged_move", lambda p, f: calls.append("staged"))
        monkeypatch.setattr(
            mover.shutil, "move", lambda s, d: (calls.append("shutil.move"), real_move(s, d))[1]
        )
        result = move_file(src, "Documents", cfg)
        assert result.moved
        assert calls == ["shutil.move"]  # fast path unchanged

    def test_cross_volume_move_takes_staged_path(self, tmp_path, monkeypatch):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("DATA", encoding="utf-8")
        calls = []
        real_move = mover.shutil.move

        def fake_staged(p, final):
            calls.append("staged")
            return real_move(str(p), str(final))  # what _staged_move ultimately does

        monkeypatch.setattr(mover, "_same_volume", lambda a, b: False)
        monkeypatch.setattr(mover, "_staged_move", fake_staged)
        monkeypatch.setattr(
            mover.shutil, "move", lambda s, d: (calls.append("shutil.move"), real_move(s, d))[1]
        )
        result = move_file(src, "Documents", cfg)
        assert result.moved
        assert calls == ["staged"]  # staged strategy chosen, never raw copy+delete
        assert (tmp_path / "out" / "Documents" / "a.txt").read_text(encoding="utf-8") == "DATA"
        assert not src.exists()
        assert not list((tmp_path / "out" / "Documents").glob("*.part"))

    def test_staged_move_transfers_content_and_removes_source(self, tmp_path):
        import fs_organizer.mover as mover

        src = tmp_path / "a.txt"
        src.write_text("STAGE-ME", encoding="utf-8")
        final_dir = tmp_path / "stage"
        final_dir.mkdir()
        mover._staged_move(src, final_dir / "a.txt")
        assert (final_dir / "a.txt").read_text(encoding="utf-8") == "STAGE-ME"
        assert not src.exists()
        assert list(final_dir.glob("*.part")) == []  # temp consumed by the rename

    def test_mid_copy_failure_leaves_source_intact_and_no_temp(self, tmp_path, monkeypatch):
        """Power loss mid-copy: intact source + no partial destination."""
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("PRECIOUS", encoding="utf-8")

        def boom(*a, **k):
            raise OSError(28, "simulated disk full mid-copy")

        monkeypatch.setattr(mover, "_same_volume", lambda a, b: False)
        monkeypatch.setattr(mover.shutil, "copyfileobj", boom)
        result = move_file(src, "Documents", cfg)
        assert result.skipped and not result.moved
        assert src.exists() and src.read_text(encoding="utf-8") == "PRECIOUS"
        assert list((tmp_path / "out" / "Documents").iterdir()) == []  # no .part survivor

    def test_failure_during_rename_deletes_temp(self, tmp_path, monkeypatch):
        """If the atomic rename fails, the staging temp must not linger."""
        import fs_organizer.mover as mover

        src = tmp_path / "a.txt"
        src.write_text("DATA", encoding="utf-8")
        final_dir = tmp_path / "stage"
        final_dir.mkdir()

        def boom(*a, **k):
            raise OSError(5, "simulated replace refusal")

        monkeypatch.setattr(mover.os, "replace", boom)
        with pytest.raises(OSError):
            mover._staged_move(src, final_dir / "a.txt")
        assert src.exists()  # source untouched
        assert list(final_dir.iterdir()) == []  # temp cleaned up

    def test_part_files_are_ignored_by_scans(self, tmp_path):
        """A leftover staging temp can never become organized input."""
        cfg = make_config(tmp_path)
        assert "*.part" in cfg.effective_ignore_patterns()


# ---------------------------------------------------------------- flaw #38
class TestFlaw38RecursiveOptIn:
    """recursive: false everywhere (default); true unlocks foldered watch dirs."""

    def _seed(self, tmp_path):
        nested = tmp_path / "watch" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (tmp_path / "watch" / "top.txt").write_text("x", encoding="utf-8")
        (nested / "deep.txt").write_text("x", encoding="utf-8")
        return nested

    def test_scan_is_top_level_only_by_default(self, tmp_path):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        self._seed(tmp_path)
        entries, _ = mover._scan_files(cfg, 100)
        assert {e["path"].name for e in entries} == {"top.txt"}

    def test_recursive_scan_includes_subfolders(self, tmp_path):
        import fs_organizer.mover as mover

        cfg = make_config(tmp_path)
        cfg.recursive = True
        self._seed(tmp_path)
        entries, _ = mover._scan_files(cfg, 100)
        assert {e["path"].name for e in entries} == {"top.txt", "deep.txt"}

    def test_one_shot_defaults_to_top_level(self, tmp_path, capsys):
        import fs_organizer.__main__ as m

        cfg = make_config(tmp_path)
        nested = self._seed(tmp_path)
        m._one_shot(cfg)
        assert (tmp_path / "out" / "Documents" / "top.txt").exists()
        assert (nested / "deep.txt").exists()  # untouched: opt-in not given

    def test_one_shot_recursive_organizes_nested(self, tmp_path, capsys):
        import fs_organizer.__main__ as m

        cfg = make_config(tmp_path)
        cfg.recursive = True
        nested = self._seed(tmp_path)
        m._one_shot(cfg)
        assert (tmp_path / "out" / "Documents" / "deep.txt").exists()
        assert not (nested / "deep.txt").exists()

    def test_recursive_one_shot_still_skips_target_root_subtree(self, tmp_path, capsys):
        """recursive + target root inside the watch folder must not loop."""
        import fs_organizer.__main__ as m

        watch = tmp_path / "watch"
        troot = watch / "Organized"
        (troot / "Documents").mkdir(parents=True)
        organized = troot / "Documents" / "already.txt"
        organized.write_text("x", encoding="utf-8")
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(
            json.dumps({
                "watch_folders": [str(watch)],
                "target_rules": {".txt": "Documents"},
                "target_root": str(troot),
                "recursive": True,
                "ai": {"enabled": False},
            }),
            encoding="utf-8",
        )
        assert m._one_shot(load_config(cfg_file)) == 0
        assert "Organized 0 file(s)" in capsys.readouterr().out
        assert organized.exists()

    def test_watcher_passes_recursive_flag_to_observer(self, tmp_path, monkeypatch):
        import fs_organizer.watcher as watcher_mod

        class FakeObserver:
            def __init__(self):
                self.scheduled = []
                self.name = ""
                self.daemon = False

            def schedule(self, handler, path, recursive=False):
                self.scheduled.append((path, recursive))

        monkeypatch.setattr(watcher_mod, "Observer", FakeObserver)

        cfg = make_config(tmp_path)
        w = watcher_mod.Watcher(cfg)
        assert w.observer.scheduled == [(str(tmp_path / "watch"), False)]

        cfg.recursive = True
        w2 = watcher_mod.Watcher(cfg)
        assert w2.observer.scheduled == [(str(tmp_path / "watch"), True)]

    def test_recursive_flag_round_trips_through_config(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)  # load_config validates existence
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(
            json.dumps({
                "watch_folders": [str(tmp_path / "watch")],
                "target_rules": {".txt": "Documents"},
                "target_root": str(tmp_path / "out"),
                "recursive": True,
                "journal": {"enabled": True, "path": str(tmp_path / "j.jsonl")},
            }),
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        assert cfg.recursive is True
        assert cfg.journal.enabled is True
        assert cfg.journal.resolved_path() == tmp_path / "j.jsonl"
        exported = cfg.to_dict()
        assert exported["recursive"] is True
        assert exported["journal"] == {"enabled": True, "path": str(tmp_path / "j.jsonl")}

    def test_recursive_must_be_boolean(self, tmp_path):
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(
            json.dumps({
                "watch_folders": [str(tmp_path / "watch")],
                "recursive": "yes",
            }),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(cfg_file)


# ---------------------------------------------------------------- flaw #39
class TestFlaw39EventSequence:
    """Monotonic gap-free seq on every event; the dashboard polls since=N."""

    def test_seq_is_monotonic_and_gap_free(self):
        from fs_organizer.ui import ActivityLog

        log = ActivityLog()
        for i in range(5):
            log.add("moved", f"f{i}.txt")
        assert [e["seq"] for e in log.snapshot()] == [4, 3, 2, 1, 0]  # newest first

    def test_seq_survives_ring_wrap(self):
        """The proven 700→500 bug class: wrapping the ring must never reset
        or reuse sequence numbers."""
        from fs_organizer.ui import ActivityLog

        log = ActivityLog(max_events=3)
        for i in range(10):
            log.add("moved", f"f{i}.txt")
        assert [e["seq"] for e in log.snapshot()] == [9, 8, 7]

    def test_since_returns_only_newer_oldest_first(self):
        from fs_organizer.ui import ActivityLog

        log = ActivityLog()
        for i in range(5):
            log.add("moved", f"f{i}.txt")
        assert [e["seq"] for e in log.since(2)] == [3, 4]
        assert log.since(99) == []
        assert [e["seq"] for e in log.since(-1)] == [0, 1, 2, 3, 4]

    def test_range_reports_retained_bounds(self):
        from fs_organizer.ui import ActivityLog

        log = ActivityLog()
        assert log.range() == (None, None)
        for i in range(5):
            log.add("moved", f"f{i}.txt")
        assert log.range() == (0, 4)

    def test_parse_since_query_param(self):
        from fs_organizer.ui import _parse_since

        assert _parse_since("/api/events?since=7") == 7
        assert _parse_since("/api/events") is None
        assert _parse_since("/api/events?since=abc") is None
        assert _parse_since("/api/events?since=-5") == 0
        assert _parse_since("/api/events?since=3&x=1") == 3

    def test_api_events_since_endpoint(self, tmp_path):
        import socket
        import urllib.request

        from fs_organizer.ui import Dashboard

        def free_port() -> int:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        def wait_for_server(url: str, timeout: float = 5.0) -> None:
            import time as _time
            import urllib.error

            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(url, timeout=2)
                    return
                except urllib.error.HTTPError:
                    return  # server is up (it answered)
                except (urllib.error.URLError, ConnectionError, OSError):
                    _time.sleep(0.05)
            raise AssertionError(f"server never came up at {url}")

        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        wait_for_server(dash.url)
        try:
            for i in range(5):
                dash.activity.add("moved", f"f{i}.txt")
            full = json.loads(
                urllib.request.urlopen(dash.url + "api/events", timeout=5).read()
            )
            assert [e["seq"] for e in full["events"]] == [4, 3, 2, 1, 0]

            inc = json.loads(
                urllib.request.urlopen(dash.url + "api/events?since=2", timeout=5).read()
            )
            assert [e["seq"] for e in inc["events"]] == [3, 4]  # oldest first, seq > 2
            assert inc["oldest"] == 0 and inc["latest"] == 4
        finally:
            dash.stop()

    def test_status_payload_exposes_recursive(self, tmp_path):
        from fs_organizer.ui import status_payload

        cfg = make_config(tmp_path)
        assert status_payload(cfg)["recursive"] is False
        cfg.recursive = True
        assert status_payload(cfg)["recursive"] is True


# ---------------------------------------------------------------- flaw #40
class TestFlaw40JournalCategory:
    """The journal entry's 'category' must be the rule/AI category that
    decided the move — not dest.parent.name, which with
    use_date_subfolders is the YYYY-MM month folder and mislabels every
    entry (the audit trail and the dashboard's first-segment-under-root
    category would disagree). An explicit category from the mover wins;
    without one the entry derives it as the FIRST segment under the
    target root, mirroring views._file_category."""

    def _journal_cfg(self, tmp_path):
        from fs_organizer.config import JournalConfig

        cfg = make_config(tmp_path)
        cfg.journal = JournalConfig(enabled=True, path=str(tmp_path / "j.jsonl"))
        return cfg

    def _rows(self, cfg):
        from fs_organizer import journal

        return journal.read_entries(cfg.journal.resolved_path())

    def test_move_with_date_subfolders_records_rule_category(self, tmp_path):
        """use_date_subfolders: dest is <root>/Documents/2026-09/a.txt;
        the entry must say 'Documents', never '2026-09'."""
        cfg = self._journal_cfg(tmp_path)
        cfg.use_date_subfolders = True
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        result = move_file(src, "Documents", cfg)
        assert result.moved
        (entry,) = self._rows(cfg)
        assert entry["category"] == "Documents"
        assert not entry["category"].startswith("20"), (
            "journal recorded the month folder as the category (flaw #40)"
        )

    def test_move_without_date_subfolders_still_correct(self, tmp_path):
        """Plain destinations keep recording the category."""
        cfg = self._journal_cfg(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        assert move_file(src, "Documents", cfg).moved
        (entry,) = self._rows(cfg)
        assert entry["category"] == "Documents"

    def test_mover_passes_explicit_category(self, tmp_path, monkeypatch):
        """append_move must be called with the deciding category, not left
        to re-derive it from a path the destination layout could change."""
        import fs_organizer.mover as mover

        cfg = self._journal_cfg(tmp_path)
        seen = {}
        real_append = mover.journal.append_move

        def spy(config, src, dest, size, category=None):
            seen["category"] = category
            return real_append(config, src, dest, size, category=category)

        monkeypatch.setattr(mover.journal, "append_move", spy)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        assert move_file(src, "Documents", cfg).moved
        assert seen["category"] == "Documents"

    def test_derived_category_is_first_segment_under_root(self, tmp_path):
        """The path-derived fallback mirrors views._file_category: first
        segment below the target root, '' for files directly in the root,
        and never the YYYY-MM month folder for date-nested destinations."""
        from fs_organizer import journal

        cfg = self._journal_cfg(tmp_path)
        root = cfg.resolved_target_root()

        nested = root / "Documents" / "2026-09" / "a.txt"
        journal.append_move(cfg, tmp_path / "a.txt", nested, 1)
        (entry,) = self._rows(cfg)
        assert entry["category"] == "Documents"

        plain = root / "Documents" / "b.txt"
        journal.append_move(cfg, tmp_path / "b.txt", plain, 1)
        rows = self._rows(cfg)
        assert rows[-1]["category"] == "Documents"

        direct = root / "loose.txt"
        journal.append_move(cfg, tmp_path / "c.txt", direct, 1)
        rows = self._rows(cfg)
        assert rows[-1]["category"] == ""

    def test_derivation_outside_root_and_broken_config_never_raises(self, tmp_path):
        """Dest outside the target root and a config whose root cannot be
        resolved both degrade to '' — journaling must never break a move."""
        from fs_organizer import journal

        cfg = self._journal_cfg(tmp_path)
        outside = tmp_path / "Elsewhere" / "x.txt"
        journal.append_move(cfg, tmp_path / "x.txt", outside, 1)
        assert self._rows(cfg)[-1]["category"] == ""

        class BadRoot:
            journal = cfg.journal

            def resolved_target_root(self):
                raise OSError("cannot resolve")

        out = tmp_path / "unrelated" / "y.txt"
        journal.append_move(BadRoot(), tmp_path / "y.txt", out, 1)
        assert journal.read_entries(cfg.journal.resolved_path())[-1]["category"] == ""

    def test_explicit_category_beats_derivation(self, tmp_path):
        """A caller-supplied category is recorded verbatim."""
        from fs_organizer import journal

        cfg = self._journal_cfg(tmp_path)
        nested = cfg.resolved_target_root() / "Documents" / "2026-09" / "z.txt"
        journal.append_move(cfg, tmp_path / "z.txt", nested, 5, category="Projects")
        (entry,) = self._rows(cfg)
        assert entry["category"] == "Projects"

    def test_dashboard_category_and_journal_agree(self, tmp_path):
        """End-to-end honesty: the dashboard's first-segment category and the
        journal's recorded category must match for a date-nested move."""
        from fs_organizer.views import files_payload

        cfg = self._journal_cfg(tmp_path)
        cfg.use_date_subfolders = True
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        assert move_file(src, "Documents", cfg).moved

        (entry,) = self._rows(cfg)
        data = files_payload(cfg)
        rows = [r for rows_ in data["groups"].values() for r in rows_]
        dashboard_category = next(r["category"] for r in rows if r["name"] == "a.txt")
        assert dashboard_category == entry["category"] == "Documents"


# ---------------------------------------------------------------- flaw #41
class TestFlaw41MatchedRulesScope:
    """watch_diag's matched_rules must come from the same bounded scan as
    its sibling counts (plan_actions), so it honors config.recursive.
    Derived from _list_children (top level only), it contradicted the very
    payload it sat in: would_organize: 5 alongside matches listing 1."""

    def _cfg(self, tmp_path, recursive):
        nested = tmp_path / "watch" / "sub"
        nested.mkdir(parents=True, exist_ok=True)
        (tmp_path / "watch" / "top.txt").write_text("x", encoding="utf-8")
        (nested / "deep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "watch" / "skip.tmp").write_text("x", encoding="utf-8")
        cfg = make_config(tmp_path)
        cfg.recursive = recursive
        return cfg

    def test_recursive_matched_rules_include_nested(self, tmp_path):
        from fs_organizer.diagnostics import watch_diag as _watch_diag

        data = _watch_diag(self._cfg(tmp_path, recursive=True), [])
        info = data["files"][str(tmp_path / "watch")]
        assert info["matched_rules"] == ["Documents", "Documents"]
        # Same-truth sanity: every matched rule corresponds to a would-move.
        assert info["would_organize"] == 2

    def test_top_level_matched_rules_unchanged(self, tmp_path):
        """recursive: false keeps the historical top-level-only result."""
        from fs_organizer.diagnostics import watch_diag as _watch_diag

        data = _watch_diag(self._cfg(tmp_path, recursive=False), [])
        info = data["files"][str(tmp_path / "watch")]
        assert info["matched_rules"] == ["Documents"]
        assert info["would_organize"] == 1

    def test_ignored_files_never_counted_as_matched(self, tmp_path):
        """A rule-matching file swallowed by the ignore list is not a match.
        (The old _list_children derivation applied the same filter, but the
        scan-derived list must keep that property too.)"""
        from fs_organizer.diagnostics import watch_diag as _watch_diag

        cfg = self._cfg(tmp_path, recursive=False)
        cfg.ignore_patterns = ["*.txt"]  # swallows every rule match
        data = _watch_diag(cfg, [])
        info = data["files"][str(tmp_path / "watch")]
        assert info["matched_rules"] == []
        assert info["would_organize"] == 0


# ---------------------------------------------------------------- flaw #42
class TestFlaw42RulesPayloadFolders:
    """rules_payload's watch_folders must be the resolved (existing) set —
    the folders the organizer actually acts on. Reporting the raw expanded
    list advertised configured-but-missing folders the watcher never
    watches (the flaw #25 bug class, in the dashboard's Rules card).
    status_payload already used resolved_watch_folders(); rules_payload is
    now consistent with it."""

    def test_missing_watch_folder_not_advertised(self, tmp_path):
        from fs_organizer.views import rules_payload

        real = tmp_path / "watch"
        real.mkdir(exist_ok=True)
        ghost = tmp_path / "ghost"
        cfg = make_config(tmp_path)
        cfg.watch_folders = [str(real), str(ghost)]

        data = rules_payload(cfg)
        assert data["watch_folders"] == [str(real)]
        # The raw list stays available under its own key, honestly labeled.
        assert str(ghost) in data["expanded_watch_folders"]

    def test_matches_status_payload_folders(self, tmp_path):
        """Both dashboard payloads must agree on the watched set."""
        from fs_organizer.ui import status_payload
        from fs_organizer.views import rules_payload

        real = tmp_path / "watch"
        real.mkdir(exist_ok=True)
        cfg = make_config(tmp_path)
        cfg.watch_folders = [str(real), str(tmp_path / "ghost")]

        assert rules_payload(cfg)["watch_folders"] == status_payload(cfg)["watch_folders"]

    def test_all_folders_exist_passthrough(self, tmp_path):
        from fs_organizer.views import rules_payload

        cfg = make_config(tmp_path)
        data = rules_payload(cfg)
        assert data["watch_folders"] == [str(tmp_path / "watch")]
        assert data["expanded_watch_folders"] == data["watch_folders"]
