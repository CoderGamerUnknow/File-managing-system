"""Property-based and fault-injection tests for the mover's safety guards.

Two complementary strategies:

**Property tests** state the invariants the mover promises and throw
generated inputs at them: no-overwrite under collisions, destination
containment for arbitrary category strings, loop-guard behavior at the
organizer, dry-run immutability, ignore/age-policy decisions, and "never
raises" for pathological inputs. Each property runs on a deterministic
seeded generator (always available, reproducible on CI) and, when
``hypothesis`` is installed, again with far deeper randomized coverage.

**Fault-injection tests** attack the failure paths directly: a copy that
dies half-way, a rename that fails, an fsync that fails, a source delete
that fails after the commit, a locked file (WinError 32), a destination
directory that cannot be created, a journal that cannot be written. The
contract is always the same - the source survives, no partial file is left
behind, and ``move_file`` returns a MoveResult instead of raising.

"""
from __future__ import annotations

import os
import random
import shutil
import threading
import time
from pathlib import Path

import pytest

from fs_organizer import journal
from fs_organizer.config import JournalConfig
from fs_organizer.mover import (
    _is_inside,
    _same_volume,
    _staged_move,
    _unique_destination,
    age_policy_allows,
    destination_for,
    move_file,
)
from fs_organizer.watcher import Organizer
from helpers import make_config

try:  # optional: deeper coverage when present, suite still green without it
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover - the absence-of-dependency branch
    HAS_HYPOTHESIS = False


# --------------------------------------------------------------------------
# generators (deterministic seeds, so a failure is always reproducible)
# --------------------------------------------------------------------------

_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}

_EXTENSIONS = ["", ".txt", ".log", ".tar.gz", ".zip", ".TXT"]

_STEMS = ["report", "budget", "notes", "IMG_2024", "a b", "unicode-Ünïcødé", "x.y"]

# Categories deliberately include traversal attempts, separators, and junk.
_CATEGORIES = [
    "Documents", "Images", "Music", "Invoices",
    "..", "../escape", "..\\..\\evil", "a/../../b", "a/b", ".", "  ", "",
]


def _rand_stem(rng: random.Random) -> str:
    stem = rng.choice(_STEMS)
    while stem.endswith((" ", ".")) or stem.split(".")[0].upper() in _RESERVED:
        stem = rng.choice(_STEMS)
    return stem


def _rand_name(rng: random.Random) -> str:
    return f"{_rand_stem(rng)}{rng.choice(_EXTENSIONS)}"


def _rand_category(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return rng.choice(_CATEGORIES)
    alphabet = "abcXYZ/\\.-_ 01"
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))


def _cfg(tmp_path: Path, name: str, **overrides):
    """Config rooted in its own subdirectory (keeps generated cases apart)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return make_config(root, **overrides)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _boom(exc):
    """A zero-arg callable that raises *exc* (for monkeypatching)."""
    def _raise(*a, **k):
        raise exc

    return _raise


# --------------------------------------------------------------------------
# properties: no-overwrite and no-loss under collisions
# --------------------------------------------------------------------------

class TestNoOverwriteProperty:
    """Invariant 1: a move never destroys an existing file."""

    def test_collisions_preserve_every_file(self, tmp_path):
        """Same-named files in different source folders must all survive."""
        for seed in range(12):
            rng = random.Random(seed)
            cfg = _cfg(tmp_path, f"collide{seed}")
            watch = cfg.resolved_watch_folders()[0]
            count = rng.randint(2, 6)
            # A tiny pool of names guarantees real destination collisions.
            names = [f"{rng.choice(['a', 'b', 'c'])}{rng.choice(_EXTENSIONS)}"
                     for _ in range(count)]

            sources = []
            for i, name in enumerate(names):
                src = _write(watch / f"src{i}" / name, f"payload-{i}")
                sources.append((src, f"payload-{i}"))

            destinations = []
            for src, _content in sources:
                result = move_file(src, "Documents", cfg)
                assert result.moved, result
                destinations.append(result.destination)

            assert len(set(destinations)) == count, (
                f"collision overwrote a file: {destinations}"
            )
            landed = sorted(p.read_text(encoding="utf-8") for p in destinations)
            assert landed == sorted(content for _src, content in sources)

    def test_preexisting_destination_is_never_clobbered(self, tmp_path):
        """The strongest form: a file already at the destination keeps its bytes."""
        for seed in range(8):
            rng = random.Random(1000 + seed)
            cfg = _cfg(tmp_path, f"pre{seed}")
            watch = cfg.resolved_watch_folders()[0]
            name = _rand_name(rng)
            sentinel = _write(cfg.resolved_target_root() / "Documents" / name, "SENTINEL")
            _write(watch / name, f"incoming-{seed}")

            result = move_file(watch / name, "Documents", cfg)
            assert result.moved
            assert sentinel.read_text(encoding="utf-8") == "SENTINEL"
            assert result.destination != sentinel

    def test_compound_extensions_stay_whole(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        first = _write(watch / "bundle.tar.gz", "payload-1")
        assert move_file(first, "Documents", cfg).moved

        second = _write(watch / "bundle.tar.gz", "payload-2")
        result = move_file(second, "Documents", cfg)
        assert result.destination.name == "bundle (1).tar.gz"
        assert result.destination.read_text(encoding="utf-8") == "payload-2"


class TestContainmentProperty:
    """Invariant: a move can never write outside the target root."""

    def test_arbitrary_categories_stay_inside_the_root(self, tmp_path):
        for seed in range(15):
            rng = random.Random(2000 + seed)
            cfg = _cfg(tmp_path, f"escape{seed}")
            watch = cfg.resolved_watch_folders()[0]
            root = cfg.resolved_target_root()
            src = _write(watch / _rand_name(rng), "payload")

            result = move_file(src, _rand_category(rng), cfg)

            if result.moved:
                assert _is_inside(result.destination, root), (
                    f"destination escaped the target root: {result.destination}"
                )
            elif result.refused:
                assert src.read_text(encoding="utf-8") == "payload"

    def test_traversal_categories_never_escape(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        root = cfg.resolved_target_root()
        for i, category in enumerate(("..", "../escape", "..\\..\\evil", "a/../../b")):
            src = _write(watch / f"file{i}.txt", "payload")
            result = move_file(src, category, cfg)
            assert not result.moved or _is_inside(result.destination, root)
        # Nothing may appear next to (or above) the target root.
        assert not (tmp_path / "escape").exists()
        assert not (root.parent / "evil").exists()


class TestLoopGuardProperty:
    """Invariant 2: organized output is never re-processed."""

    def test_organizer_leaves_its_own_output_alone(self, tmp_path):
        for seed in range(6):
            rng = random.Random(3000 + seed)
            cfg = _cfg(tmp_path, f"loop{seed}", recursive=True)
            depth = "/".join(rng.choice(["Documents", "Images", "2026-10"])
                             for _ in range(rng.randint(1, 3)))
            organized = _write(
                cfg.resolved_target_root() / depth / _rand_name(rng), "organized"
            )

            Organizer(cfg).handle(organized)

            assert organized.exists(), "the loop guard moved the organizer's own output"
            assert organized.read_text(encoding="utf-8") == "organized"


class TestDryRunProperty:
    """Invariant: dry_run moves nothing at all."""

    def test_dry_run_is_side_effect_free(self, tmp_path):
        for seed in range(10):
            rng = random.Random(4000 + seed)
            cfg = _cfg(tmp_path, f"dry{seed}", dry_run=True)
            watch = cfg.resolved_watch_folders()[0]
            paths = [
                _write(watch / _rand_name(rng), f"c-{i}")
                for i in range(rng.randint(1, 4))
            ]

            for path in paths:
                result = move_file(path, "Documents", cfg)
                assert result.would_move, result
                assert path.exists(), "dry_run moved the source"
                assert not result.destination.exists()
            # Not even the destination directory may appear.
            assert not cfg.resolved_target_root().exists()


class TestPolicyProperties:
    def test_ignored_files_are_never_moved(self, tmp_path):
        cfg = make_config(tmp_path, ignore_patterns=["*.tmp", "*~"])
        watch = tmp_path / "watch"
        for i, name in enumerate(("a.tmp", "b.tmp", "c~")):
            path = _write(watch / name, "x")
            result = move_file(path, "Documents", cfg)
            assert result.skipped and result.reason == "ignored", name
            assert path.exists()

    def test_age_policy_boundary(self, tmp_path):
        from fs_organizer.config import AgePolicy

        # move_file() evaluates the policy against the real clock, so the
        # mtimes below are relative to it (and min_age is wide enough that a
        # slow machine cannot drift across the boundary mid-test).
        min_age = 3600.0
        cfg = make_config(tmp_path, age_policy=AgePolicy(min_age_seconds=min_age))
        watch = tmp_path / "watch"
        old = _write(watch / "old.txt", "x")
        fresh = _write(watch / "fresh.txt", "x")
        now = time.time()
        os.utime(old, (now - 2 * min_age, now - 2 * min_age))  # old enough
        os.utime(fresh, (now, now))                          # still being written

        assert age_policy_allows(old, cfg, now=now)
        assert not age_policy_allows(fresh, cfg, now=now)

        result = move_file(fresh, "Documents", cfg)
        assert result.skipped and result.reason == "outside age policy"
        assert fresh.exists()
        assert move_file(old, "Documents", cfg).moved

    def test_move_file_never_raises_on_pathological_inputs(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        vanished = _write(watch / "vanishing.txt", "x")
        vanished.unlink()
        spaced = _write(watch / "spaced  name.txt", "x")

        cases = [
            (watch / "does-not-exist.txt", "Documents"),
            (vanished, "Documents"),
            (spaced, "Documents"),
            (watch / "does-not-exist.txt", "../escape"),
            (watch / "does-not-exist.txt", ""),
        ]
        for path, category in cases:
            result = move_file(path, category, cfg)  # must not raise
            assert result is not None, (path, category)


# --------------------------------------------------------------------------
# fault injection: the failure paths
# --------------------------------------------------------------------------

class TestStagedMoveFaults:
    """The staged (cross-volume) path must never lose or half-commit a file."""

    @staticmethod
    def _stage(tmp_path):
        src = _write(tmp_path / "src.bin", "precious payload")
        final = tmp_path / "dest" / "dst.bin"
        final.parent.mkdir(parents=True, exist_ok=True)
        return src, final

    @staticmethod
    def _assert_clean_abort(src, final):
        assert src.exists(), "source was destroyed by a failed staged move"
        assert not final.exists(), "a partial/failed destination was committed"
        assert not final.with_name(final.name + ".part").exists(), (
            "staging file left behind"
        )

    def test_copy_dying_halfway_keeps_the_source(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        src, final = self._stage(tmp_path)

        def dying_copy(fsrc, fdst, length=None):
            fdst.write(b"half a file")
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(mover.shutil, "copyfileobj", dying_copy)
        with pytest.raises(OSError):
            _staged_move(src, final)
        self._assert_clean_abort(src, final)
        assert src.read_text(encoding="utf-8") == "precious payload"

    def test_rename_failure_keeps_the_source(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        src, final = self._stage(tmp_path)
        monkeypatch.setattr(mover.os, "replace",
                            _boom(OSError("sharing violation on rename")))
        with pytest.raises(OSError):
            _staged_move(src, final)
        self._assert_clean_abort(src, final)

    def test_fsync_failure_aborts_before_the_commit(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        src, final = self._stage(tmp_path)
        monkeypatch.setattr(mover.os, "fsync", _boom(OSError("fsync unsupported")))
        with pytest.raises(OSError):
            _staged_move(src, final)
        self._assert_clean_abort(src, final)

    def test_failed_source_delete_keeps_the_committed_copy(self, tmp_path, monkeypatch):
        """The rename already committed: report success, never undo the move."""
        src, final = self._stage(tmp_path)
        real_unlink = Path.unlink

        def selective_unlink(self, *a, **k):
            if self == src:
                raise OSError("source locked after copy (simulated)")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", selective_unlink)
        _staged_move(src, final)  # must NOT raise: the copy is committed
        assert final.read_text(encoding="utf-8") == "precious payload"
        assert src.exists()  # the delete failed, so the source is still here

    def test_move_file_survives_a_failing_staged_copy(self, tmp_path, monkeypatch):
        """End-to-end through move_file on the cross-volume (staged) path."""
        from fs_organizer import mover

        cfg = make_config(tmp_path)
        src = _write(tmp_path / "watch" / "big.bin", "precious payload")
        monkeypatch.setattr(mover, "_same_volume", lambda a, b: False)

        def dying_copy(fsrc, fdst, length=None):
            fdst.write(b"half")
            raise OSError("cable unplugged (simulated)")

        monkeypatch.setattr(mover.shutil, "copyfileobj", dying_copy)
        result = move_file(src, "Documents", cfg)

        assert result.skipped and result.reason.startswith("OS error")
        assert src.read_text(encoding="utf-8") == "precious payload"
        assert not (cfg.resolved_target_root() / "Documents" / "big.bin").exists()


class TestMoveFileFaults:
    def test_locked_file_is_reported_as_transient(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        cfg = make_config(tmp_path)
        src = _write(tmp_path / "watch" / "locked.txt", "x")
        locked = PermissionError(32, "The process cannot access the file")
        locked.winerror = 32  # ERROR_SHARING_VIOLATION
        monkeypatch.setattr(mover.shutil, "move", _boom(locked))

        result = move_file(src, "Documents", cfg)

        assert result.skipped and result.transient
        assert result.reason == "temporarily locked"
        assert src.exists()

    def test_permission_denied_is_a_plain_skip(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        cfg = make_config(tmp_path)
        src = _write(tmp_path / "watch" / "denied.txt", "x")
        monkeypatch.setattr(mover.shutil, "move", _boom(PermissionError("denied")))

        result = move_file(src, "Documents", cfg)

        assert result.skipped and not result.transient
        assert "permission denied" in result.reason
        assert src.exists()

    def test_same_file_error_is_a_skip(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        cfg = make_config(tmp_path)
        src = _write(tmp_path / "watch" / "same.txt", "x")
        monkeypatch.setattr(mover.shutil, "move",
                            _boom(shutil.SameFileError("same file")))

        result = move_file(src, "Documents", cfg)

        assert result.skipped and result.reason == "already at destination"
        assert src.exists()

    def test_vanished_file_is_skipped(self, tmp_path):
        cfg = make_config(tmp_path)
        result = move_file(tmp_path / "watch" / "ghost.txt", "Documents", cfg)
        assert result.skipped and result.reason == "file vanished"

    def test_uncreatable_destination_leaves_the_source(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        src = _write(tmp_path / "watch" / "x.txt", "payload")
        real_mkdir = Path.mkdir

        def failing_mkdir(self, *a, **k):
            if self.name == "Documents":
                raise PermissionError("cannot create directory (simulated)")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        result = move_file(src, "Documents", cfg)

        assert result.skipped and "permission denied" in result.reason
        assert src.read_text(encoding="utf-8") == "payload"

    def test_unwritable_journal_never_breaks_the_move(self, tmp_path):
        """Invariant: the journal is best-effort - the move still completes."""
        cfg = make_config(tmp_path)
        journal_path = tmp_path / "not-a-file"
        journal_path.mkdir()  # a DIRECTORY where a journal file is expected
        cfg.journal = JournalConfig(enabled=True, path=str(journal_path))
        src = _write(tmp_path / "watch" / "j.txt", "payload")

        result = move_file(src, "Documents", cfg)

        assert result.moved
        assert result.destination.read_text(encoding="utf-8") == "payload"


class TestConcurrencyFault:
    def test_parallel_same_name_moves_all_survive(self, tmp_path):
        """The per-destination-directory lock is what makes this safe (flaw #1).

        On Windows a parallel burst can also draw transient
        ERROR_SHARING_VIOLATIONs (antivirus), which the mover reports as
        ``transient`` for the caller to retry. The test mirrors the daemon:
        retry the reported transients, and require that every file either
        landed intact or is still at its source - never lost, never
        overwritten, never silently dropped.
        """
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        count = 8
        sources = []
        for i in range(count):
            sources.append(_write(watch / f"src{i}" / "same-name.txt", f"payload-{i}"))

        results: dict[int, object] = {}
        errors: list[Exception] = []

        def worker(idx, src):
            try:
                results[idx] = move_file(src, "Documents", cfg)
            except Exception as exc:  # noqa: BLE001 - ANY escape from a worker
                errors.append(exc)  # is the failure under test, by design

        def run_pass() -> list[int]:
            threads = [threading.Thread(target=worker, args=(i, s))
                       for i, s in enumerate(sources)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, f"a concurrent move raised: {errors}"
            return [i for i, r in results.items() if getattr(r, "transient", False)]

        # Nothing may be lost or overwritten on the FIRST pass, regardless of
        # transient locks: every payload is either landed or still at source.
        def assert_nothing_lost() -> None:
            dest = cfg.resolved_target_root() / "Documents"
            landed = [p.read_text(encoding="utf-8") for p in dest.iterdir()]
            for i, src in enumerate(sources):
                assert (f"payload-{i}" in landed) or src.exists(), (
                    f"payload-{i} vanished - the mover lost a file"
                )

        pending = run_pass()
        assert_nothing_lost()
        for _attempt in range(5):  # bounded retry, exactly as Organizer does
            if not pending:
                break
            pending = [
                i for i in pending
                if not (move_file(sources[i], "Documents", cfg).moved)
            ]
        assert_nothing_lost()
        assert not pending, f"transient locks never cleared: {pending}"

        landed = sorted(
            p.read_text(encoding="utf-8")
            for p in (cfg.resolved_target_root() / "Documents").iterdir()
        )
        assert landed == sorted(f"payload-{i}" for i in range(count))


# --------------------------------------------------------------------------
# guard invariants themselves (fast, always run)
# --------------------------------------------------------------------------

def test_same_volume_matches_drive_prefix(tmp_path):
    assert _same_volume(tmp_path, tmp_path / "sub") is True


def test_destination_for_uses_template_when_configured(tmp_path):
    from fs_organizer.config import DestinationTemplate

    cfg = make_config(
        tmp_path,
        destination_template=DestinationTemplate(pattern="{category}/{date:%Y}"),
    )
    src = _write(tmp_path / "watch" / "a.txt", "x")
    dest = destination_for(src, "Documents", cfg, mtime=1767000000.0)
    assert dest.name == "2025"


def test_journal_records_the_deciding_category(tmp_path):
    """flaw #40: the recorded category is the rule's, not the date folder."""
    cfg = make_config(tmp_path, use_date_subfolders=True)
    cfg.journal = JournalConfig(enabled=True, path=str(tmp_path / "moves.jsonl"))
    src = _write(tmp_path / "watch" / "a.txt", "x")

    assert move_file(src, "Documents", cfg).moved

    entries = journal.read_entries(cfg.journal.resolved_path())
    assert [e["category"] for e in entries] == ["Documents"]


# --------------------------------------------------------------------------
# deeper randomized coverage when hypothesis is installed
# --------------------------------------------------------------------------

if HAS_HYPOTHESIS:
    SAFE_NAME = (
        st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,24}", fullmatch=True)
        .filter(lambda s: not s.endswith((" ", ".")))
        .filter(lambda s: s.split(".")[0].upper() not in _RESERVED)
    )
    ANY_CATEGORY = st.one_of(
        st.sampled_from(_CATEGORIES),
        st.text(alphabet="abcXYZ/\\.-_ 01", max_size=12),
    )
    HYPOTHESIS_SETTINGS = settings(
        max_examples=25,
        deadline=None,  # real filesystem I/O; a per-example deadline is flaky
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )

    class TestMoverPropertiesHypothesis:
        """The same invariants, generated by hypothesis (deeper search than seeds)."""

        @HYPOTHESIS_SETTINGS
        @given(stems=st.lists(SAFE_NAME, min_size=1, max_size=5),
               ext=st.sampled_from(_EXTENSIONS))
        def test_all_collided_files_survive(self, tmp_path, stems, ext):
            cfg = make_config(tmp_path)
            watch = tmp_path / "watch"
            names = [f"{stem}{ext}" for stem in stems]

            sources = []
            for i, name in enumerate(names):
                src = _write(watch / f"src{i}" / name, f"c{i}")
                sources.append((src, f"c{i}"))

            destinations = [
                move_file(src, "Documents", cfg).destination for src, _c in sources
            ]

            assert len(set(destinations)) == len(sources)
            assert sorted(p.read_text(encoding="utf-8") for p in destinations) == sorted(
                content for _src, content in sources
            )

        @HYPOTHESIS_SETTINGS
        @given(name=SAFE_NAME, category=ANY_CATEGORY)
        def test_no_category_escapes_the_target_root(self, tmp_path, name, category):
            cfg = make_config(tmp_path)
            root = cfg.resolved_target_root()
            src = _write(tmp_path / "watch" / name, "payload")

            result = move_file(src, category, cfg)

            if result.moved:
                assert _is_inside(result.destination, root)
                assert result.destination.read_text(encoding="utf-8") == "payload"
            elif result.refused:
                assert src.read_text(encoding="utf-8") == "payload"

        @HYPOTHESIS_SETTINGS
        @given(name=SAFE_NAME)
        def test_unique_destination_is_always_free(self, tmp_path, name):
            existing = _write(tmp_path / "out" / "Documents" / name, "here")
            candidate = _unique_destination(existing)
            assert candidate != existing
            assert not candidate.exists()

        @HYPOTHESIS_SETTINGS
        @given(name=SAFE_NAME)
        def test_dry_run_never_touches_disk(self, tmp_path, name):
            cfg = make_config(tmp_path, dry_run=True)
            src = _write(tmp_path / "watch" / name, "payload")

            result = move_file(src, "Documents", cfg)

            assert result.would_move
            assert src.exists()
            assert not cfg.resolved_target_root().exists()

else:  # pragma: no cover - informational skip, not a failure

    @pytest.mark.skip(reason="hypothesis not installed; seeded properties ran")
    def test_hypothesis_properties_need_hypothesis():
        pass