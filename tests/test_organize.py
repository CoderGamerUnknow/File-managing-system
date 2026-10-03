"""Tests for the on-demand `organize` command (V2).

Acting on explicitly named paths OUTSIDE the watch folders, on demand.

Every test here builds its own throwaway tree under pytest's ``tmp_path``
and writes synthetic content into it. No test reads, moves, or writes any
real user file, and none touches anything outside its own tmp directory.
"""
import json

import pytest

from fs_organizer.__main__ import main, organize_paths
from fs_organizer.config import AgePolicy
from helpers import make_config


def _cfg(tmp_path, **overrides):
    """A config whose watch folder is deliberately NOT where the files are.

    The point of the feature is reaching outside the watch folders, so the
    fixture keeps ``watch/`` empty and puts the action in ``outside/``.
    """
    (tmp_path / "watch").mkdir(exist_ok=True)
    (tmp_path / "outside").mkdir(exist_ok=True)
    cfg = make_config(tmp_path, rules={".pdf": "Documents", ".txt": "Documents"}, **overrides)
    return cfg


class TestOrganizeOutsideWatchFolders:
    def test_organizes_a_file_outside_the_watch_folder(self, tmp_path):
        """The headline capability: a named path outside every watch folder."""
        cfg = _cfg(tmp_path)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(stray)])

        assert counts["moved"] == 1
        assert (tmp_path / "out" / "Documents" / "a.pdf").is_file()
        assert not stray.exists()

    def test_watch_folder_itself_is_untouched_by_the_command(self, tmp_path):
        """Reach is per-invocation: naming one path enrolls nothing."""
        cfg = _cfg(tmp_path)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")
        organize_paths(cfg, [str(stray)])

        # A file sitting in the watch folder was NOT swept up by the above.
        watched = tmp_path / "watch" / "b.pdf"
        watched.write_text("y", encoding="utf-8")
        assert watched.is_file()

    def test_organizes_a_folder_argument(self, tmp_path):
        cfg = _cfg(tmp_path)
        folder = tmp_path / "outside"
        for name in ("a.pdf", "b.pdf", "c.txt"):
            (folder / name).write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(folder)])

        assert counts["moved"] == 3
        assert (tmp_path / "out" / "Documents" / "a.pdf").is_file()
        assert not (folder / "a.pdf").exists()

    def test_folder_is_top_level_only_by_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        nested = tmp_path / "outside" / "deep"
        nested.mkdir()
        (nested / "buried.pdf").write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(tmp_path / "outside")])

        assert counts["moved"] == 0
        assert nested / "buried.pdf" in [nested / "buried.pdf"]
        assert (nested / "buried.pdf").is_file()

    def test_recursive_flag_reaches_subfolders(self, tmp_path):
        cfg = _cfg(tmp_path)
        nested = tmp_path / "outside" / "deep"
        nested.mkdir()
        (nested / "buried.pdf").write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(tmp_path / "outside")], recursive=True)

        assert counts["moved"] == 1
        assert (tmp_path / "out" / "Documents" / "buried.pdf").is_file()

    def test_sub_rules_apply_to_paths_outside_the_watch_folders(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            sub_rules=[{"pattern": "**/invoices/**", "extensions": [".pdf"], "category": "Invoices"}],
        )
        inv = tmp_path / "outside" / "invoices"
        inv.mkdir()
        target = inv / "bill.pdf"
        target.write_text("x", encoding="utf-8")

        organize_paths(cfg, [str(target)])

        assert (tmp_path / "out" / "Invoices" / "bill.pdf").is_file()

    def test_missing_path_is_reported_not_fatal(self, tmp_path):
        """One bad argument must not abort the rest of the batch."""
        cfg = _cfg(tmp_path)
        good = tmp_path / "outside" / "a.pdf"
        good.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(tmp_path / "nope.pdf"), str(good)])

        assert counts["moved"] == 1
        assert (tmp_path / "out" / "Documents" / "a.pdf").is_file()

    def test_unmatched_file_is_left_in_place(self, tmp_path):
        cfg = _cfg(tmp_path)
        odd = tmp_path / "outside" / "thing.xyz"
        odd.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(odd)])

        assert counts["moved"] == 0
        assert counts["unmatched"] == 1
        assert odd.is_file()

    def test_overlapping_arguments_do_not_double_move(self, tmp_path):
        cfg = _cfg(tmp_path)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        # The folder AND the file inside it.
        counts = organize_paths(cfg, [str(tmp_path / "outside"), str(stray)])

        assert counts["moved"] == 1


class TestOrganizeKeepsEveryGuard:
    """The on-demand path must be no weaker than the watched one."""

    def test_refuses_to_reorganize_its_own_output(self, tmp_path):
        """Loop guard. Without it: Documents/Documents/Documents forever."""
        cfg = _cfg(tmp_path)
        organized = tmp_path / "out" / "Documents" / "already.pdf"
        organized.parent.mkdir(parents=True)
        organized.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(organized)])

        assert counts["moved"] == 0
        assert organized.is_file()
        assert not (tmp_path / "out" / "Documents" / "Documents").exists()

    def test_refuses_the_whole_target_root_subtree(self, tmp_path):
        cfg = _cfg(tmp_path)
        deep = tmp_path / "out" / "Documents" / "2026" / "a.pdf"
        deep.parent.mkdir(parents=True)
        deep.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(tmp_path / "out")], recursive=True)

        assert counts["moved"] == 0
        assert deep.is_file()

    def test_never_overwrites_an_existing_file(self, tmp_path):
        """Same-name collision must suffix, not destroy the incumbent."""
        cfg = _cfg(tmp_path)
        incumbent = tmp_path / "out" / "Documents" / "a.pdf"
        incumbent.parent.mkdir(parents=True)
        incumbent.write_text("ORIGINAL", encoding="utf-8")
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("NEW", encoding="utf-8")

        counts = organize_paths(cfg, [str(stray)])

        assert counts["moved"] == 1
        assert incumbent.read_text(encoding="utf-8") == "ORIGINAL"
        siblings = sorted(p.name for p in (tmp_path / "out" / "Documents").iterdir())
        assert len(siblings) == 2  # original + "a (1).pdf"

    def test_ignore_patterns_still_apply(self, tmp_path):
        cfg = _cfg(tmp_path)
        ignored = tmp_path / "outside" / "draft.tmp"
        ignored.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(ignored)])

        assert counts["moved"] == 0
        assert ignored.is_file()

    def test_age_policy_still_applies(self, tmp_path):
        import os
        import time

        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            age_policy=AgePolicy(min_age_seconds=3600, max_age_days=None),
        )
        (tmp_path / "watch").mkdir(exist_ok=True)
        (tmp_path / "outside").mkdir(exist_ok=True)
        fresh = tmp_path / "outside" / "just-written.pdf"
        fresh.write_text("x", encoding="utf-8")
        now = time.time()
        os.utime(fresh, (now, now))  # brand new -> inside min_age

        counts = organize_paths(cfg, [str(fresh)])

        assert counts["moved"] == 0
        assert fresh.is_file()

    def test_destination_escaping_the_target_root_is_refused(self, tmp_path):
        """A category like '../Outside' must never escape containment."""
        cfg = _cfg(tmp_path)
        cfg.target_rules = {".pdf": "../Escaped"}
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(stray)])

        assert counts["moved"] == 0
        assert counts["refused"] == 1
        assert stray.is_file()
        assert not (tmp_path.parent / "Escaped").exists()

    def test_dry_run_moves_nothing(self, tmp_path):
        cfg = make_config(tmp_path, rules={".pdf": "Documents"}, dry_run=True)
        (tmp_path / "outside").mkdir(exist_ok=True)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        counts = organize_paths(cfg, [str(stray)])

        assert counts["would_move"] == 1
        assert counts["moved"] == 0
        assert stray.is_file()


class TestOrganizeCli:
    def _write_cfg(self, tmp_path, **overrides):
        (tmp_path / "watch").mkdir(exist_ok=True)
        (tmp_path / "outside").mkdir(exist_ok=True)
        data = {
            "watch_folders": [str(tmp_path / "watch")],
            "target_rules": {".pdf": "Documents"},
            "target_root": str(tmp_path / "out"),
            "dry_run": False,
            "ai": {"enabled": False},
        }
        data.update(overrides)
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_cli_organizes_a_named_path(self, tmp_path, capsys):
        cfg = self._write_cfg(tmp_path)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        rc = main([str(cfg), "organize", str(stray)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "Moved 1" in out
        assert (tmp_path / "out" / "Documents" / "a.pdf").is_file()

    def test_cli_dry_run_flag_overrides_config(self, tmp_path, capsys):
        cfg = self._write_cfg(tmp_path, dry_run=False)
        stray = tmp_path / "outside" / "a.pdf"
        stray.write_text("x", encoding="utf-8")

        rc = main([str(cfg), "organize", str(stray), "--dry-run"])

        assert rc == 0
        assert stray.is_file()
        assert not (tmp_path / "out" / "Documents" / "a.pdf").exists()
        assert "would move 1" in capsys.readouterr().out

    def test_cli_recursive_flag(self, tmp_path):
        cfg = self._write_cfg(tmp_path)
        nested = tmp_path / "outside" / "deep"
        nested.mkdir()
        (nested / "buried.pdf").write_text("x", encoding="utf-8")

        main([str(cfg), "organize", str(tmp_path / "outside"), "--recursive"])

        assert (tmp_path / "out" / "Documents" / "buried.pdf").is_file()

    def test_cli_requires_at_least_one_path(self, tmp_path):
        cfg = self._write_cfg(tmp_path)
        with pytest.raises(SystemExit):
            main([str(cfg), "organize"])

    def test_cli_reports_refusals(self, tmp_path, capsys):
        cfg = self._write_cfg(tmp_path)
        organized = tmp_path / "out" / "Documents" / "already.pdf"
        organized.parent.mkdir(parents=True)
        organized.write_text("x", encoding="utf-8")

        rc = main([str(cfg), "organize", str(organized)])

        assert rc == 0
        assert organized.is_file()

    def test_bad_config_exits_2(self, tmp_path, capsys):
        p = tmp_path / "bad.json"
        p.write_text("{}", encoding="utf-8")
        rc = main([str(p), "organize", str(tmp_path)])
        assert rc == 2