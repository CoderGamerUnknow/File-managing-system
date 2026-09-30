"""Tests for the one-shot CLI scan mode and exit codes."""
import json
import logging

import pytest

from fs_organizer.__main__ import _build_log_handlers, _one_shot, main


def write_cfg(tmp_path, **overrides):
    watch = tmp_path / "watch"
    watch.mkdir(exist_ok=True)
    data = {
        "watch_folders": [str(watch)],
        "target_rules": {".txt": "Documents"},
        "ignore_patterns": ["*.tmp"],
        "target_root": str(tmp_path / "out"),
        "dry_run": False,
        "file_stable_seconds": 0.1,
        "ai": {"enabled": False},
    }
    data.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p, watch


def test_one_shot_organizes_files(tmp_path, capsys):
    p, watch = write_cfg(tmp_path)
    (watch / "a.txt").write_text("x", encoding="utf-8")
    (watch / "b.txt").write_text("y", encoding="utf-8")

    rc = _one_shot(_load(p))
    out = capsys.readouterr().out
    assert rc == 2  # returns the number of files actually moved
    assert "Organized 2 file(s)" in out
    assert (tmp_path / "out" / "Documents" / "a.txt").exists()
    assert not (watch / "a.txt").exists()


def _load(path):
    from fs_organizer.config import load_config

    return load_config(path)


def test_one_shot_skips_ignored(tmp_path, capsys):
    p, watch = write_cfg(tmp_path)
    (watch / "keep.tmp").write_text("x", encoding="utf-8")
    (watch / "a.txt").write_text("x", encoding="utf-8")

    rc = _one_shot(_load(p))
    assert rc == 1  # one file moved; the ignored one stays
    assert (watch / "keep.tmp").exists()
    assert (tmp_path / "out" / "Documents" / "a.txt").exists()


def test_one_shot_counts_nothing_when_empty(tmp_path, capsys):
    p, watch = write_cfg(tmp_path)
    rc = _one_shot(_load(p))
    assert rc == 0
    assert "Organized 0 file(s)" in capsys.readouterr().out


def test_main_bad_config_exit_code(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    rc = main([str(bad), "--once"])
    assert rc == 2
    # Message goes to stdout via _print (stderr is unavailable under pythonw).
    assert "Config error" in capsys.readouterr().out


def test_main_missing_config_file(tmp_path, capsys):
    rc = main([str(tmp_path / "nope.json"), "--once"])
    assert rc == 2


def test_main_missing_config_arg(capsys):
    with pytest.raises(SystemExit):
        main([])


def test_one_shot_dry_run_leaves_everything(tmp_path, capsys):
    p, watch = write_cfg(tmp_path, dry_run=True)
    (watch / "a.txt").write_text("x", encoding="utf-8")

    rc = _one_shot(_load(p))
    assert rc == 0
    assert (watch / "a.txt").exists()
    assert not (tmp_path / "out" / "Documents").exists()


def test_log_file_rotates_instead_of_growing_forever(tmp_path):
    log = tmp_path / "logs" / "app.log"
    handlers = _build_log_handlers(str(log), max_bytes=500, backup_count=2)
    try:
        logger = logging.getLogger("rotation-probe")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in handlers:
            logger.addHandler(h)

        for i in range(50):
            logger.info("line %03d %s", i, "x" * 60)
        for h in handlers:
            h.flush()

        # Capped at max_bytes; rollovers produced .1 and .2, nothing beyond.
        assert log.stat().st_size <= 500
        assert (tmp_path / "logs" / "app.log.1").exists()
        assert (tmp_path / "logs" / "app.log.2").exists()
        assert not (tmp_path / "logs" / "app.log.3").exists()
    finally:
        for h in handlers:
            h.close()
