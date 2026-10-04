"""V2 Phase 1: runtime stats, pause/resume, live reload, single-instance guard."""
from __future__ import annotations

import json
import time

import pytest

from fs_organizer.runtime import InstanceLock
from fs_organizer.watcher import Watcher
from helpers import make_config


# ------------------------------------------------------------- runtime stats
class TestRuntimeStats:
    def test_counters_follow_events(self, tmp_path):
        from fs_organizer.ui import ActivityLog

        log = ActivityLog()
        log.add("moved", "a.txt", "-> out")
        log.add("moved", "b.txt", "-> out")
        log.add("skipped", "c.tmp")
        log.add("refused", "d.txt", "escapes root")
        log.add("error", "e.txt", "AI failed")
        log.add("info", "(scan)", "uncounted kind")
        s = log.stats_snapshot()
        assert s["moved"] == 2
        assert s["skipped"] == 1
        assert s["refused"] == 1
        assert s["errors"] == 1
        assert s["dryrun"] == 0
        assert s["uptime_seconds"] >= 0

    def test_bytes_and_ai_counters(self, tmp_path):
        from fs_organizer.ui import ActivityLog

        log = ActivityLog()
        log.record_bytes(100)
        log.record_bytes(23)
        log.record_ai_call()
        s = log.stats_snapshot()
        assert s["bytes_moved"] == 123
        assert s["ai_calls"] == 1

    def test_organizer_increments_bytes_and_ai(self, tmp_path, monkeypatch):
        from fs_organizer.ui import ActivityLog
        from fs_organizer.watcher import Organizer

        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.extensions = [".zzz"]
        cfg.ai.allowed_subfolders = ["Documents"]
        activity = ActivityLog()
        import fs_organizer.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "classify_with_ai", lambda p, c: "Documents")
        org = Organizer(cfg, activity=activity)

        src = tmp_path / "watch" / "m.zzz"
        src.write_text("12345", encoding="utf-8")
        org.handle(src)
        s = activity.stats_snapshot()
        assert s["ai_calls"] == 1
        assert s["bytes_moved"] == 5
        assert s["moved"] == 1

    def test_status_payload_includes_stats_and_paused(self, tmp_path):
        import json
        import urllib.request

        from fs_organizer.ui import Dashboard

        def free_port():
            import socket

            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.activity.add("moved", "x.txt")
        dash.start()
        try:
            data = json.loads(urllib.request.urlopen(dash.url + "api/status", timeout=5).read())
            assert data["stats"]["moved"] == 1
            assert data["paused"] is False
            assert "uptime_seconds" in data["stats"]
        finally:
            dash.stop()


# ------------------------------------------------------------- pause / resume
class TestPauseResume:
    def test_pause_holds_then_resume_dispatches(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.file_stable_seconds = 0.1
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        watcher = Watcher(cfg)
        watcher.pause()
        assert watcher.paused is True
        watcher.start()
        try:
            watcher.debouncer.schedule(src)
            time.sleep(0.5)
            # Held, not moved: the file must still exist.
            assert src.exists(), "paused watcher moved a file"
            watcher.resume()
            deadline = time.monotonic() + 10
            while src.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not src.exists(), "held file was not dispatched on resume"
            assert (tmp_path / "out" / "Documents" / "a.txt").exists()
        finally:
            watcher.stop()

    def test_pause_state_reported_in_activity(self, tmp_path):
        from fs_organizer.ui import ActivityLog

        cfg = make_config(tmp_path)
        activity = ActivityLog()
        watcher = Watcher(cfg, activity=activity)
        watcher.pause()
        assert activity.stats_snapshot()["paused"] is True
        watcher.resume()
        assert activity.stats_snapshot()["paused"] is False

    def test_stop_while_paused_drains_held(self, tmp_path):
        """A paused watcher shut down must not strand held paths."""
        cfg = make_config(tmp_path)
        cfg.file_stable_seconds = 0.1
        src = tmp_path / "watch" / "b.txt"
        src.write_text("x", encoding="utf-8")

        watcher = Watcher(cfg)
        watcher.pause()
        watcher.start()
        watcher.debouncer.schedule(src)
        time.sleep(0.4)  # debouncer fired -> held
        watcher.stop()   # must release held paths before draining
        assert not src.exists()
        assert (tmp_path / "out" / "Documents" / "b.txt").exists()

    def test_pause_endpoint(self, tmp_path):
        import json
        import socket
        import urllib.request

        from fs_organizer.ui import Dashboard

        def free_port():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        cfg = make_config(tmp_path)
        watcher = Watcher(cfg)
        dash = Dashboard(cfg, port=free_port(), open_browser=False, watcher=watcher)
        dash.start()
        try:
            req = urllib.request.Request(
                dash.url + "api/pause",
                data=json.dumps({"action": "pause"}).encode(),
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            assert resp["ok"] is True and resp["paused"] is True
            assert watcher.paused is True

            req = urllib.request.Request(
                dash.url + "api/pause",
                data=json.dumps({"action": "resume"}).encode(),
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            assert resp["ok"] is True and resp["paused"] is False

            # Bad action rejected.
            req = urllib.request.Request(
                dash.url + "api/pause",
                data=json.dumps({"action": "explode"}).encode(),
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError("bad pause action accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
        finally:
            dash.stop()


# ------------------------------------------------------------- live reload
class TestLiveReload:
    def test_apply_config_swaps_watches(self, tmp_path, monkeypatch):
        import fs_organizer.watcher as watcher_mod

        class FakeObserver:
            def __init__(self):
                self.scheduled = []
                self.name = ""
                self.daemon = False

            def schedule(self, handler, path, recursive=False):
                self.scheduled.append((path, recursive))

            def unschedule_all(self):
                self.scheduled.clear()

        monkeypatch.setattr(watcher_mod, "Observer", FakeObserver)
        cfg = make_config(tmp_path)
        watcher = watcher_mod.Watcher(cfg)

        other = tmp_path / "watch2"
        other.mkdir()
        new_cfg = make_config(tmp_path)
        new_cfg.watch_folders = [str(other)]
        watcher.apply_config(new_cfg)

        assert watcher.config is new_cfg
        assert watcher.organizer.config is new_cfg
        assert watcher.observer.scheduled == [(str(other), False)]

        # The handler's canonical key list must follow the roots:
        # _in_watch_roots() compares KEYS only, so a stale key list would
        # reject every event from the newly watched folder and keep
        # accepting events from the old one after a dashboard reload.
        from fs_organizer.rules import normalize_path_key

        assert watcher.handler._watch_keys == [normalize_path_key(other.resolve())]
        assert watcher.handler._in_watch_roots(other / "new.txt") is True
        assert watcher.handler._in_watch_roots(tmp_path / "watch" / "old.txt") is False

    def test_apply_config_failure_restores_old(self, tmp_path, monkeypatch):
        import fs_organizer.watcher as watcher_mod

        class FakeObserver:
            def __init__(self):
                self.name = ""
                self.daemon = False
                self.calls = []

            def schedule(self, handler, path, recursive=False):
                if "bad" in str(path):
                    raise OSError("cannot watch")
                self.calls.append(("schedule", path))

            def unschedule_all(self):
                self.calls.append(("unschedule_all",))

        monkeypatch.setattr(watcher_mod, "Observer", FakeObserver)
        cfg = make_config(tmp_path)
        watcher = watcher_mod.Watcher(cfg)

        bad = tmp_path / "watch-bad"
        bad.mkdir()
        new_cfg = make_config(tmp_path)
        new_cfg.watch_folders = [str(bad)]
        with pytest.raises(OSError):
            watcher.apply_config(new_cfg)
        # Old config object restored as the active one.
        assert watcher.config is cfg

    def test_reload_endpoint_rejects_invalid_and_keeps_old(self, tmp_path):
        import json
        import socket
        import urllib.request

        from fs_organizer.ui import Dashboard

        def free_port():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        cfg_path = tmp_path / "c.json"
        watch = tmp_path / "watch"
        watch.mkdir(exist_ok=True)  # load_config validates existence
        cfg_path.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "target_rules": {".txt": "Documents"},
            "target_root": str(tmp_path / "out"),
            "ai": {"enabled": False},
        }), encoding="utf-8")
        from fs_organizer.config import load_config

        cfg = load_config(cfg_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False, config_path=cfg_path)
        dash.start()
        try:
            # Break the file: rules without a leading dot.
            cfg_path.write_text(json.dumps({
                "watch_folders": [str(watch)],
                "target_rules": {"txt": "Documents"},
                "target_root": str(tmp_path / "out"),
                "ai": {"enabled": False},
            }), encoding="utf-8")
            req = urllib.request.Request(
                dash.url + "api/reload", data=b"{}",
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            try:
                resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            except urllib.error.HTTPError as exc:
                assert exc.code == 409  # rejected reload -> conflict status
                resp = json.loads(exc.read())
            assert resp["ok"] is False
            assert "stays active" in resp["message"]
            assert dash.config is cfg  # old config untouched

            # Fix the file with a new rule; reload applies it.
            cfg_path.write_text(json.dumps({
                "watch_folders": [str(watch)],
                "target_rules": {".txt": "Documents", ".png": "Images"},
                "target_root": str(tmp_path / "out"),
                "ai": {"enabled": False},
            }), encoding="utf-8")
            req2 = urllib.request.Request(
                dash.url + "api/reload", data=b"{}",
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req2, timeout=5).read())
            assert resp["ok"] is True
            assert dash.config.target_rules == {".txt": "Documents", ".png": "Images"}
            assert "Images" in resp["status"]["categories"]
        finally:
            dash.stop()

    def test_reload_without_config_path_is_graceful(self, tmp_path):
        import json
        import socket
        import urllib.request

        from fs_organizer.ui import Dashboard

        def free_port():
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        dash = Dashboard(make_config(tmp_path), port=free_port(), open_browser=False)
        dash.start()
        try:
            req = urllib.request.Request(
                dash.url + "api/reload", data=b"{}",
                headers={"Content-Type": "application/json", "X-Auth-Token": dash.state.token},
                method="POST",
            )
            try:
                resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            except urllib.error.HTTPError as exc:
                assert exc.code == 409
                resp = json.loads(exc.read())
            assert resp["ok"] is False
        finally:
            dash.stop()


# ------------------------------------------------------------- single instance
class TestSingleInstance:
    def test_acquire_release_cycle(self, tmp_path):
        lock = InstanceLock(tmp_path / "c.lock")
        ok, _ = lock.acquire()
        assert ok
        assert lock.probe()["state"] == "held"
        assert lock.probe()["pid"] == _pid()
        lock.release()
        assert lock.probe()["state"] == "free"

    def test_second_acquire_refused(self, tmp_path):
        first = InstanceLock(tmp_path / "c.lock")
        ok, _ = first.acquire()
        assert ok
        second = InstanceLock(tmp_path / "c.lock")
        ok, msg = second.acquire()
        assert ok is False
        assert "already running" in msg
        first.release()

    def test_force_overrides_held(self, tmp_path):
        first = InstanceLock(tmp_path / "c.lock")
        first.acquire()
        second = InstanceLock(tmp_path / "c.lock")
        ok, _ = second.acquire(force=True)
        assert ok
        second.release()

    def test_stale_lock_replaced(self, tmp_path, monkeypatch):
        lock = InstanceLock(tmp_path / "c.lock")
        lock.path.write_text("999999", encoding="utf-8")  # dead pid
        assert lock.probe()["state"] == "stale"
        ok, _ = lock.acquire()
        assert ok, "stale lock must be replaceable"
        lock.release()

    def test_garbage_lock_treated_stale(self, tmp_path):
        lock = InstanceLock(tmp_path / "c.lock")
        lock.path.write_text("not-a-pid", encoding="utf-8")
        assert lock.probe()["state"] == "stale"

    def test_context_manager_releases(self, tmp_path):
        with InstanceLock(tmp_path / "c.lock") as lock:
            assert lock.probe()["state"] == "held"
        assert lock.probe()["state"] == "free"

    def test_daemon_refuses_second_instance_end_to_end(self, tmp_path, capsys):
        """main() exits 3 when another instance holds the config's lock."""
        import fs_organizer.__main__ as m

        watch = tmp_path / "watch"
        watch.mkdir()
        cfg = tmp_path / "c.json"
        cfg.write_text(json.dumps({
            "watch_folders": [str(watch)],
            "target_rules": {".txt": "Documents"},
            "target_root": str(tmp_path / "out"),
            "ai": {"enabled": False},
        }), encoding="utf-8")

        blocker = InstanceLock(cfg.with_suffix(".lock"))
        ok, _ = blocker.acquire()
        assert ok
        try:
            # A stop event that is immediately set so, with --force, the
            # daemon would exit at once — but without force we must never
            # get that far.
            rc = m.main([str(cfg), "--verbose"])
            assert rc == 3
            assert "Not starting" in capsys.readouterr().out
        finally:
            blocker.release()


def _pid() -> int:
    import os

    return os.getpid()
