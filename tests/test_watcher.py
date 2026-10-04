"""Tests for the Organizer decision logic and event handler wiring."""
import time
from pathlib import Path

from fs_organizer.watcher import Organizer, Watcher, _EventHandler
from helpers import make_config


def make_file(tmp_path, name, text="x"):
    p = tmp_path / "watch" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestOrganizerHandle:
    def test_moves_file_by_rule(self, tmp_path):
        cfg = make_config(tmp_path)
        src = make_file(tmp_path, "a.txt")
        Organizer(cfg).handle(src)
        assert not src.exists()
        assert (tmp_path / "out" / "Documents" / "a.txt").exists()

    def test_ignores_matching_patterns(self, tmp_path):
        cfg = make_config(tmp_path)
        src = make_file(tmp_path, "a.tmp")
        Organizer(cfg).handle(src)
        assert src.exists()

    def test_no_rule_no_ai_leaves_file(self, tmp_path):
        cfg = make_config(tmp_path)
        src = make_file(tmp_path, "mystery.zzz")
        Organizer(cfg).handle(src)
        assert src.exists()

    def test_skips_non_files(self, tmp_path):
        cfg = make_config(tmp_path)
        d = tmp_path / "watch" / "subdir.txt"  # a directory, not a file
        d.mkdir()
        Organizer(cfg).handle(d)
        assert d.exists()  # untouched

    def test_never_touches_target_root(self, tmp_path):
        """Files already inside the target root must not be re-organized (loop guard)."""
        cfg = make_config(tmp_path)
        already_moved = tmp_path / "out" / "Documents" / "a.txt"
        already_moved.parent.mkdir(parents=True)
        already_moved.write_text("x", encoding="utf-8")
        Organizer(cfg).handle(already_moved)
        assert already_moved.exists()

    def test_ai_used_only_for_configured_extensions(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.provider = "ollama"
        cfg.ai.extensions = [".xyz"]
        cfg.ai.allowed_subfolders = ["Documents"]

        src = make_file(tmp_path, "mystery.zzz")  # not in ai.extensions
        called = {"n": 0}

        def fake_classify(path, ai_cfg):
            called["n"] += 1
            return "Documents"

        import fs_organizer.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "classify_with_ai", fake_classify)
        Organizer(cfg).handle(src)
        assert called["n"] == 0, "AI must not be called for extensions outside ai.extensions"
        assert src.exists()

    def test_ai_success_moves_file(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.provider = "ollama"
        cfg.ai.extensions = [".zzz"]
        cfg.ai.allowed_subfolders = ["Documents"]

        src = make_file(tmp_path, "mystery.zzz")

        import fs_organizer.watcher as watcher_mod

        monkeypatch.setattr(
            watcher_mod, "classify_with_ai", lambda path, ai_cfg: "Documents"
        )
        Organizer(cfg).handle(src)
        assert not src.exists()
        assert (tmp_path / "out" / "Documents" / "mystery.zzz").exists()

    def test_ai_failure_leaves_file(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.provider = "ollama"
        cfg.ai.extensions = [".zzz"]
        cfg.ai.allowed_subfolders = ["Documents"]

        src = make_file(tmp_path, "mystery.zzz")

        import fs_organizer.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "classify_with_ai", lambda path, ai_cfg: None)
        Organizer(cfg).handle(src)
        assert src.exists()


class TestEventHandler:
    def test_created_file_schedules_debounce(self, tmp_path):
        class FakeDebouncer:
            def __init__(self):
                self.scheduled = []

            def schedule(self, path):
                self.scheduled.append(path)

        from watchdog.events import FileCreatedEvent, FileMovedEvent

        d = FakeDebouncer()
        handler = _EventHandler(Organizer(make_config(tmp_path)), d, [tmp_path / "watch"])

        handler.on_created(FileCreatedEvent(str(tmp_path / "watch" / "a.txt")))
        assert d.scheduled == [Path(str(tmp_path / "watch" / "a.txt"))]

        # Destination must be inside a watched folder to be scheduled.
        handler.on_moved(FileMovedEvent(str(tmp_path / "b.tmp"), str(tmp_path / "watch" / "b.txt")))
        assert d.scheduled[-1] == Path(str(tmp_path / "watch" / "b.txt"))

    def test_directory_events_not_scheduled(self, tmp_path):
        class FakeDebouncer:
            def __init__(self):
                self.scheduled = []

            def schedule(self, path):
                self.scheduled.append(path)

        from watchdog.events import DirCreatedEvent

        d = FakeDebouncer()
        handler = _EventHandler(Organizer(make_config(tmp_path)), d, [tmp_path / "watch"])
        handler.on_created(DirCreatedEvent(str(tmp_path / "watch" / "subdir")))
        assert d.scheduled == []


class TestDispatchKey:
    def test_alias_spellings_share_one_key(self, tmp_path):
        """resolve() folds ALIASES of one file (a '..' segment, a symlinked
        watch folder) into one key; normalize_path_key then folds the
        extended-path/case spelling resolve() emits intermittently. Dropping
        either half re-opens flaw #51's family: one file, two keys, two
        dispatched moves and a 'a (1).txt' copy."""
        p = make_file(tmp_path, "a.txt")
        alias = tmp_path / "watch" / ".." / "watch" / "a.txt"
        assert Watcher._dispatch_key(p) == Watcher._dispatch_key(alias)

        # Distinct files keep distinct keys.
        other = make_file(tmp_path, "b.txt")
        assert Watcher._dispatch_key(p) != Watcher._dispatch_key(other)


class TestResumeDispatchesOriginalSpelling:
    def test_held_map_stores_scheduled_path_not_folded_key(self, tmp_path):
        """resume() must dispatch the path AS SCHEDULED. Rebuilding it from
        the canonical key hands the mover a case-folded src.name, and the
        destination is named after src.name — a pause/resume would then
        RENAME the file to lowercase on Windows (the journal-category case
        bug's twin)."""
        cfg = make_config(tmp_path)
        cfg.file_stable_seconds = 0.05
        src = make_file(tmp_path, "MiXeD.txt")

        watcher = Watcher(cfg)
        watcher.start()
        try:
            watcher.pause()
            watcher.debouncer.schedule(src)
            deadline = time.monotonic() + 5
            while not watcher._held and time.monotonic() < deadline:
                time.sleep(0.05)
            assert watcher._held, "path was not held while paused"

            ((key, held_path),) = watcher._held.items()
            assert held_path.name == "MiXeD.txt", (
                f"held map lost the original spelling (stored {held_path.name!r}); "
                "resume would rename the file"
            )
            assert key  # canonical key is present alongside the value

            watcher.resume()
            deadline = time.monotonic() + 10
            while src.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not src.exists(), "resumed path was not dispatched"
            assert (tmp_path / "out" / "Documents" / "MiXeD.txt").exists()
        finally:
            watcher.stop()
