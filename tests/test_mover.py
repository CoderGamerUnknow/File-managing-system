"""Tests for the file mover: destinations, collisions, dry-run."""
import logging
from pathlib import Path

import pytest

from fs_organizer.mover import _unique_destination, destination_for, move_file

from helpers import make_config


def test_move_into_category_folder(tmp_path):
    cfg = make_config(tmp_path)
    src = tmp_path / "watch" / "a.txt"
    src.write_text("hello", encoding="utf-8")

    result = move_file(src, "Documents", cfg)

    assert result.moved is True
    dest = result.destination
    assert dest == tmp_path / "out" / "Documents" / "a.txt"
    assert dest.exists() and dest.read_text(encoding="utf-8") == "hello"
    assert not src.exists()


def test_move_creates_nested_dirs(tmp_path):
    cfg = make_config(tmp_path)
    src = tmp_path / "watch" / "a.txt"
    src.write_text("x", encoding="utf-8")

    result = move_file(src, "Deep/Nested/Cat", cfg)
    assert result.moved and result.destination.exists()


def test_collision_gets_suffixed(tmp_path):
    cfg = make_config(tmp_path)
    out = tmp_path / "out" / "Documents"
    out.mkdir(parents=True)
    (out / "a.txt").write_text("original", encoding="utf-8")

    src = tmp_path / "watch" / "a.txt"
    src.write_text("new", encoding="utf-8")

    result = move_file(src, "Documents", cfg)
    dest = result.destination
    assert dest == out / "a (1).txt"
    assert (out / "a.txt").read_text(encoding="utf-8") == "original"
    assert dest.read_text(encoding="utf-8") == "new"


def test_repeated_collisions_increment(tmp_path):
    cfg = make_config(tmp_path)
    out = tmp_path / "out" / "Documents"
    out.mkdir(parents=True)
    (out / "a.txt").write_text("0", encoding="utf-8")
    (out / "a (1).txt").write_text("1", encoding="utf-8")

    src = tmp_path / "watch" / "a.txt"
    src.write_text("2", encoding="utf-8")
    result = move_file(src, "Documents", cfg)
    assert result.destination == out / "a (2).txt"


def test_unique_destination_direct(tmp_path):
    p = tmp_path / "f.txt"
    assert _unique_destination(p) == p  # free
    p.write_text("x", encoding="utf-8")
    assert _unique_destination(p) == tmp_path / "f (1).txt"
    (tmp_path / "f (1).txt").write_text("x", encoding="utf-8")
    assert _unique_destination(p) == tmp_path / "f (2).txt"


def test_dry_run_moves_nothing(tmp_path, caplog):
    cfg = make_config(tmp_path, dry_run=True)
    src = tmp_path / "watch" / "a.txt"
    src.write_text("hello", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="fs_organizer"):
        result = move_file(src, "Documents", cfg)

    assert result.moved is False
    assert result.would_move is True, "dry-run must report what WOULD happen"
    assert src.exists(), "dry-run must not move the file"
    assert not (tmp_path / "out" / "Documents" / "a.txt").exists()
    assert "[dry-run]" in caplog.text


def test_ignored_file_skipped(tmp_path):
    cfg = make_config(tmp_path)
    src = tmp_path / "watch" / "a.tmp"
    src.write_text("x", encoding="utf-8")

    result = move_file(src, "Documents", cfg)
    assert result.skipped is True and result.moved is False
    assert src.exists()


def test_vanished_file_skipped(tmp_path):
    cfg = make_config(tmp_path)
    ghost = tmp_path / "watch" / "gone.txt"  # never created
    result = move_file(ghost, "Documents", cfg)
    assert result.skipped is True and result.moved is False


def test_permission_error_skipped(tmp_path, monkeypatch):
    import shutil

    cfg = make_config(tmp_path)
    src = tmp_path / "watch" / "a.txt"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    result = move_file(src, "Documents", cfg)
    assert result.skipped is True
    assert src.exists()


def test_destination_with_date_subfolders(tmp_path):
    cfg = make_config(tmp_path, use_date_subfolders=True)
    src = tmp_path / "watch" / "a.txt"
    src.write_text("x", encoding="utf-8")

    dest_dir = destination_for(src, "Documents", cfg)
    assert dest_dir.parent == tmp_path / "out" / "Documents"
    assert len(dest_dir.name) == 7 and dest_dir.name[4] == "-"  # YYYY-MM

    result = move_file(src, "Documents", cfg)
    assert result.moved and result.destination.parent == dest_dir
