"""Shared test helpers for the fs-organizer test suite."""
from __future__ import annotations

from fs_organizer.config import Config


def make_config(tmp_path, rules=None, **overrides) -> Config:
    """Standard single-watch-folder Config with ``watch/`` pre-created.

    ``rules`` defaults to the minimal ``.txt -> Documents`` map most tests
    use; ``overrides`` are applied as attribute sets on the built Config.
    """
    (tmp_path / "watch").mkdir(exist_ok=True)
    cfg = Config(
        watch_folders=[str(tmp_path / "watch")],
        target_rules=rules if rules is not None else {".txt": "Documents"},
        ignore_patterns=["*.tmp"],
        target_root=str(tmp_path / "out"),
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
