"""Tests for config loading and validation."""
import json

import pytest

from fs_organizer.config import ConfigError, load_config


def write_cfg(tmp_path, data):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def base_config(tmp_path):
    watch = tmp_path / "watch"
    watch.mkdir(exist_ok=True)
    return {
        "watch_folders": [str(watch)],
        "target_rules": {".txt": "Text"},
        "ignore_patterns": ["*.tmp"],
        "target_root": str(tmp_path / "out"),
        "dry_run": False,
        "file_stable_seconds": 0.1,
        "ai": {"enabled": False},
    }


def test_loads_minimal_config(tmp_path):
    cfg = load_config(write_cfg(tmp_path, base_config(tmp_path)))
    assert cfg.watch_folders == [str(tmp_path / "watch")]
    assert cfg.target_rules == {".txt": "Text"}
    assert cfg.file_stable_seconds == 0.1
    assert cfg.ai.enabled is False


def test_missing_watch_folders_raises(tmp_path):
    data = base_config(tmp_path)
    del data["watch_folders"]
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, data))


def test_nonexistent_watch_folder_raises(tmp_path):
    data = base_config(tmp_path)
    data["watch_folders"] = [str(tmp_path / "nope")]
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(write_cfg(tmp_path, data))


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        load_config(p)


def test_rule_keys_must_start_with_dot(tmp_path):
    data = base_config(tmp_path)
    data["target_rules"] = {"txt": "Text"}
    with pytest.raises(ConfigError, match="extension starting with"):
        load_config(write_cfg(tmp_path, data))


def test_extension_case_normalized(tmp_path):
    data = base_config(tmp_path)
    data["target_rules"] = {".TXT": "Text"}
    cfg = load_config(write_cfg(tmp_path, data))
    assert cfg.target_rules == {".txt": "Text"}


def test_openai_requires_api_key(tmp_path):
    data = base_config(tmp_path)
    data["ai"] = {"enabled": True, "provider": "openai"}
    with pytest.raises(ConfigError, match="api_key"):
        load_config(write_cfg(tmp_path, data))


def test_invalid_provider_raises(tmp_path):
    data = base_config(tmp_path)
    data["ai"] = {"enabled": True, "provider": "claude"}
    with pytest.raises(ConfigError, match="provider"):
        load_config(write_cfg(tmp_path, data))


def test_dry_run_flag(tmp_path):
    data = base_config(tmp_path)
    data["dry_run"] = True
    cfg = load_config(write_cfg(tmp_path, data))
    assert cfg.dry_run is True
