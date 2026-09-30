"""V2 Phase 2: file-age policies and destination templates."""
from __future__ import annotations

import json
import os
import time

from fs_organizer.config import AgePolicy, ConfigError, DestinationTemplate, load_config
from fs_organizer.mover import age_policy_allows, destination_for, move_file, plan_actions

from helpers import make_config


# ------------------------------------------------------------- age policy
class TestAgePolicy:
    def test_no_policy_means_everything_allowed(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        assert age_policy_allows(src, cfg) is True

    def test_min_age_blocks_too_new(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(min_age_seconds=3600)
        src = tmp_path / "watch" / "fresh.txt"
        src.write_text("x", encoding="utf-8")
        assert age_policy_allows(src, cfg) is False

    def test_min_age_passes_old_enough(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(min_age_seconds=60)
        src = tmp_path / "watch" / "old.txt"
        src.write_text("x", encoding="utf-8")
        old = time.time() - 3600
        os.utime(src, (old, old))
        assert age_policy_allows(src, cfg) is True

    def test_max_age_blocks_too_old(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(max_age_days=30)
        src = tmp_path / "watch" / "ancient.txt"
        src.write_text("x", encoding="utf-8")
        ancient = time.time() - 31 * 86400
        os.utime(src, (ancient, ancient))
        assert age_policy_allows(src, cfg) is False

    def test_max_age_passes_recent(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(max_age_days=30)
        src = tmp_path / "watch" / "recent.txt"
        src.write_text("x", encoding="utf-8")
        assert age_policy_allows(src, cfg) is True

    def test_move_file_skips_outside_age_policy(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(max_age_days=1)
        src = tmp_path / "watch" / "ancient.txt"
        src.write_text("precious", encoding="utf-8")
        ancient = time.time() - 10 * 86400
        os.utime(src, (ancient, ancient))

        result = move_file(src, "Documents", cfg)
        assert result.skipped is True and result.moved is False
        assert src.exists(), "age policy must leave the file in place"
        assert "age" in result.reason

    def test_vanished_file_allows(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(min_age_seconds=9999)
        assert age_policy_allows(tmp_path / "watch" / "ghost.txt", cfg) is True

    def test_config_roundtrip(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "age_policy": {"min_age_seconds": 5, "max_age_days": 365},
        }), encoding="utf-8")
        cfg = load_config(cfg_file)
        assert cfg.age_policy.min_age_seconds == 5.0
        assert cfg.age_policy.max_age_days == 365.0
        exported = cfg.to_dict()["age_policy"]
        assert exported == {"min_age_seconds": 5.0, "max_age_days": 365.0}

    def test_invalid_age_policy_rejected(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "age_policy": {"min_age_seconds": -3},
        }), encoding="utf-8")
        try:
            load_config(cfg_file)
            raise AssertionError("negative min_age accepted")
        except ConfigError as exc:
            assert "age_policy.min_age_seconds" in str(exc)

    def test_age_policy_must_be_object(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "age_policy": 7,
        }), encoding="utf-8")
        try:
            load_config(cfg_file)
            raise AssertionError("non-object age_policy accepted")
        except ConfigError as exc:
            assert "'age_policy' must be an object" in str(exc)

    def test_one_shot_respects_age_policy(self, tmp_path, capsys):
        from fs_organizer.__main__ import _one_shot

        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(max_age_days=1)
        src = tmp_path / "watch" / "ancient.txt"
        src.write_text("x", encoding="utf-8")
        ancient = time.time() - 10 * 86400
        os.utime(src, (ancient, ancient))
        _one_shot(cfg)
        assert src.exists(), "one-shot moved an age-policy-protected file"


# --------------------------------------------------- destination templates
class TestDestinationTemplate:
    def test_default_template_is_plain_category(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        dest = destination_for(src, "Documents", cfg)
        assert dest == tmp_path / "out" / "Documents"

    def test_use_date_subfolders_still_works(self, tmp_path):
        cfg = make_config(tmp_path, use_date_subfolders=True)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        dest = destination_for(src, "Documents", cfg)
        assert dest.parent == tmp_path / "out" / "Documents"
        assert len(dest.name) == 7 and dest.name[4] == "-"  # YYYY-MM

    def test_template_year_month(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.destination_template = DestinationTemplate(pattern="{category}/{date:%Y}/{date:%Y-%m}")
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        dest = destination_for(src, "Documents", cfg)
        rel = dest.relative_to(cfg.resolved_target_root())
        parts = rel.parts
        assert parts[0] == "Documents"
        assert len(parts[1]) == 4  # year
        assert len(parts[2]) == 7 and parts[2][4] == "-"  # YYYY-MM

    def test_template_extra_text(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.destination_template = DestinationTemplate(pattern="sorted/{category}-files")
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        dest = destination_for(src, "Documents", cfg)
        assert dest == cfg.resolved_target_root() / "sorted" / "Documents-files"

    def test_move_with_template_lands_correctly(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.destination_template = DestinationTemplate(pattern="{category}/{date:%Y}")
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        result = move_file(src, "Documents", cfg)
        assert result.moved
        year = time.strftime("%Y")
        assert result.destination == cfg.resolved_target_root() / "Documents" / year / "a.txt"

    def test_template_wins_over_date_subfolders(self, tmp_path):
        cfg = make_config(tmp_path, use_date_subfolders=True)
        cfg.destination_template = DestinationTemplate(pattern="{category}/bucket")
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")
        dest = destination_for(src, "Documents", cfg)
        assert dest == cfg.resolved_target_root() / "Documents" / "bucket"

    def test_template_requires_category_token(self):
        try:
            DestinationTemplate.from_dict({"pattern": "one-big-folder"})
            raise AssertionError("pattern without {category} accepted")
        except ConfigError as exc:
            assert "{category}" in str(exc)

    def test_template_rejects_absolute(self):
        try:
            DestinationTemplate.from_dict({"pattern": "/etc/{category}"})
            raise AssertionError("absolute pattern accepted")
        except ConfigError as exc:
            assert "relative" in str(exc)

    def test_template_rejects_empty(self):
        try:
            DestinationTemplate.from_dict({"pattern": "  "})
            raise AssertionError("empty pattern accepted")
        except ConfigError:
            pass

    def test_template_must_be_object(self):
        try:
            DestinationTemplate.from_dict("category")
            raise AssertionError("non-object template accepted")
        except ConfigError as exc:
            assert "must be an object" in str(exc)

    def test_traversal_category_refused_with_template(self, tmp_path):
        """The mover's traversal guard (#12) must hold under templates."""
        cfg = make_config(tmp_path)
        cfg.destination_template = DestinationTemplate(pattern="{category}/{date:%Y}")
        src = tmp_path / "watch" / "evil.txt"
        src.write_text("x", encoding="utf-8")
        outside = tmp_path / "Outside"
        result = move_file(src, "../../Outside", cfg)
        assert result.skipped is True and result.refused is True
        assert src.exists() and not outside.exists()

    def test_plan_reports_template_destination(self, tmp_path):
        from fs_organizer.mover import plan

        cfg = make_config(tmp_path)
        cfg.destination_template = DestinationTemplate(pattern="{category}/{date:%Y}")
        (tmp_path / "watch" / "a.txt").write_text("x", encoding="utf-8")
        data = plan(cfg)
        (row,) = data["rows"]
        assert row["category"] == "Documents"  # the deciding category (#43)
        assert row["destination"].parts[-2] == time.strftime("%Y")

    def test_plan_actions_age_bucket(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(max_age_days=1)
        src = tmp_path / "watch" / "ancient.txt"
        src.write_text("x", encoding="utf-8")
        ancient = time.time() - 10 * 86400
        os.utime(src, (ancient, ancient))
        data = plan_actions(cfg)
        assert data["counts"]["would_skip_age"] == 1
        assert data["counts"]["would_organize"] == 0

    def test_dashboard_payloads_carry_new_fields(self, tmp_path):
        from fs_organizer.views import status_payload

        cfg = make_config(tmp_path)
        cfg.age_policy = AgePolicy(min_age_seconds=10)
        cfg.destination_template = DestinationTemplate(pattern="{category}/{date:%Y}")
        s = status_payload(cfg)
        assert "age_policy" in s
        assert "destination_template" in s
