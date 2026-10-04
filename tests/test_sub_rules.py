"""Tests for V2 per-category sub-rules (source-folder-aware routing)."""
import json

import pytest

from fs_organizer.config import ConfigError, SubRule, load_config
from fs_organizer.mover import plan_actions
from fs_organizer.watcher import Organizer
from helpers import make_config


def _subrule(pattern, extensions, category):
    return {"pattern": pattern, "extensions": extensions, "category": category}


class TestSubRuleParsing:
    def test_sub_rules_parse_and_normalize(self, tmp_path):
        cfg = make_config(
            tmp_path,
            sub_rules=[_subrule(str(tmp_path / "watch" / "inv") + "/**", [".PDF"], "Invoices")],
        )
        assert len(cfg.sub_rules) == 1
        rule = cfg.sub_rules[0]
        assert rule.extensions == [".pdf"]  # lower-cased
        assert rule.category == "Invoices"
        assert rule.pattern.endswith("/**")

    def test_sub_rules_must_be_a_list(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "sub_rules": {"pattern": "x"},
        }), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(cfg_file)

    @pytest.mark.parametrize("bad", [
        {},  # missing everything
        {"pattern": "x/**", "extensions": [".pdf"]},  # no category
        {"pattern": "x/**", "category": "Invoices"},  # no extensions
        {"pattern": "x/**", "extensions": [], "category": "Invoices"},  # empty ext list
        {"pattern": "x/**", "extensions": ["pdf"], "category": "Invoices"},  # no dot
        {"category": "Invoices", "extensions": [".pdf"]},  # no pattern
        {"pattern": "", "extensions": [".pdf"], "category": "Invoices"},  # empty pattern
        {"pattern": "x/**", "extensions": [".pdf"], "category": "  "},  # blank category
    ])
    def test_invalid_sub_rules_rejected(self, tmp_path, bad):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "sub_rules": [bad],
        }), encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(cfg_file)

    def test_sub_rules_round_trip_through_config(self, tmp_path):
        (tmp_path / "watch").mkdir(exist_ok=True)
        cfg_file = tmp_path / "c.json"
        cfg_file.write_text(json.dumps({
            "watch_folders": [str(tmp_path / "watch")],
            "sub_rules": [_subrule("x/**", [".pdf"], "Invoices")],
        }), encoding="utf-8")
        cfg = load_config(cfg_file)
        exported = cfg.to_dict()
        assert exported["sub_rules"] == [
            {"pattern": "x/**", "extensions": [".pdf"], "category": "Invoices"}
        ]

    def test_no_sub_rules_exports_empty_list(self, tmp_path):
        cfg = make_config(tmp_path)
        assert cfg.to_dict()["sub_rules"] == []


class TestCategoryFor:
    def test_sub_rule_wins_over_global_table(self, tmp_path):
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
        )
        f = tmp_path / "watch" / "invoices" / "a.pdf"
        f.write_text("x")
        assert cfg.category_for(f) == "Invoices"

    def test_global_table_applies_outside_the_sub_rule(self, tmp_path):
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
        )
        f = tmp_path / "watch" / "elsewhere.pdf"
        f.write_text("x")
        assert cfg.category_for(f) == "Documents"

    def test_extension_gate_is_required(self, tmp_path):
        """A path match alone must not reroute an extension the rule omits."""
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents", ".txt": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
        )
        f = tmp_path / "watch" / "invoices" / "a.txt"
        f.write_text("x")
        assert cfg.category_for(f) == "Documents"

    def test_first_matching_rule_wins(self, tmp_path):
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[
                _subrule("**/invoices/**", [".pdf"], "First"),
                _subrule("**/invoices/**", [".pdf"], "Second"),
            ],
        )
        f = tmp_path / "watch" / "invoices" / "a.pdf"
        f.write_text("x")
        assert cfg.category_for(f) == "First"

    def test_bare_pattern_matches_file_name_only(self, tmp_path):
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("invoice-*", [".pdf"], "Invoices")],
        )
        hit = tmp_path / "watch" / "invoice-a.pdf"
        hit.write_text("x")
        (tmp_path / "watch" / "b.pdf").write_text("x")
        assert cfg.category_for(hit) == "Invoices"
        assert cfg.category_for(tmp_path / "watch" / "b.pdf") == "Documents"

    def test_windows_backslash_pattern_matches(self, tmp_path):
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**\\invoices\\**", [".pdf"], "Invoices")],
        )
        f = tmp_path / "watch" / "invoices" / "a.pdf"
        f.write_text("x")
        assert cfg.category_for(f) == "Invoices"

    def test_no_sub_rules_falls_back_to_table(self, tmp_path):
        cfg = make_config(tmp_path, rules={".txt": "Documents"})
        f = tmp_path / "watch" / "a.txt"
        f.write_text("x")
        assert cfg.category_for(f) == "Documents"

    def test_unknown_extension_still_none(self, tmp_path):
        cfg = make_config(
            tmp_path,
            rules={".txt": "Documents"},
            sub_rules=[_subrule("**/x/**", [".pdf"], "Invoices")],
        )
        f = tmp_path / "watch" / "a.dat"
        f.write_text("x")
        assert cfg.category_for(f) is None


class TestPlansAndOneShot:
    def test_plan_actions_reports_sub_rule_category_and_destination(self, tmp_path):
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
            recursive=True,
        )
        (tmp_path / "watch" / "invoices" / "a.pdf").write_text("x")
        actions = plan_actions(cfg)
        assert actions["counts"]["would_organize"] == 1
        row = actions["would_organize"][0]
        assert row["category"] == "Invoices"
        assert row["destination"].name == "a.pdf"
        assert "Invoices" in str(row["destination"])

    def test_plan_uses_sub_rule_destination(self, tmp_path):
        from fs_organizer.mover import plan

        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
            recursive=True,
        )
        (tmp_path / "watch" / "invoices" / "a.pdf").write_text("x")
        data = plan(cfg)
        assert data["total"] == 1
        assert data["rows"][0]["category"] == "Invoices"
        assert "Invoices" in str(data["rows"][0]["destination"])

    def test_one_shot_organizes_via_sub_rule(self, tmp_path, capsys):
        from fs_organizer.__main__ import _one_shot

        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
            recursive=True,
        )
        src = tmp_path / "watch" / "invoices" / "a.pdf"
        src.write_text("x")
        assert _one_shot(cfg) == 1
        moved = tmp_path / "out" / "Invoices" / "a.pdf"
        assert moved.is_file()
        assert not src.exists()


class TestOrganizerSubRules:
    def test_live_organizer_routes_via_sub_rule(self, tmp_path):
        (tmp_path / "watch" / "invoices").mkdir(parents=True)
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("**/invoices/**", [".pdf"], "Invoices")],
        )
        src = tmp_path / "watch" / "invoices" / "a.pdf"
        src.write_text("x")
        Organizer(cfg).handle(src)
        assert (tmp_path / "out" / "Invoices" / "a.pdf").is_file()
        assert not src.exists()


class TestTildeExpansion:
    """``~`` in a sub-rule pattern expands like every other path field.

    Regression guard: ``category_for()`` used to fnmatch the pattern
    verbatim against an absolute path, so a documented ``~/Downloads/**``
    example silently matched nothing. ``watch_folders`` and ``target_root``
    have always expanded ``~``; sub-rules must agree.
    """

    def test_tilde_slash_pattern_matches_under_home(self, tmp_path, monkeypatch):
        """The canonical case: ``~/x/**`` routes files in the real home."""
        home = tmp_path / "home"
        (home / "Downloads" / "invoices").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))  # Windows
        src = home / "Downloads" / "invoices" / "a.pdf"
        src.write_text("x")
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("~/Downloads/invoices/**", [".pdf"], "Invoices")],
        )
        assert cfg.category_for(src) == "Invoices"

    def test_tilde_pattern_does_not_match_a_different_folder(self, tmp_path, monkeypatch):
        """Expansion must not turn the rule into a match-everything glob."""
        home = tmp_path / "home"
        (home / "Downloads" / "invoices").mkdir(parents=True)
        (home / "Documents").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("~/Downloads/invoices/**", [".pdf"], "Invoices")],
        )
        assert cfg.category_for(home / "Documents" / "a.pdf") == "Documents"

    def test_backslash_tilde_pattern_expands(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "Downloads" / "invoices").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        src = home / "Downloads" / "invoices" / "a.pdf"
        src.write_text("x")
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("~\\Downloads\\invoices\\**", [".pdf"], "Invoices")],
        )
        assert cfg.category_for(src) == "Invoices"

    def test_backslash_tilde_pattern_expands_under_posix_expanduser(
        self, tmp_path, monkeypatch
    ):
        """Linux CI's failure mode, reproduced on every platform.

        ``posixpath.expanduser`` only recognizes ``~`` at the start of a
        string or ``~/...``: a backslash is a legal *filename* character on
        POSIX, not a separator, so ``~\\Downloads\\...`` comes back
        untouched. Expansion must therefore happen AFTER separators are
        folded, or the rule silently never matches on Linux and the file
        falls back to the global category (CI: ubuntu py3.9 + py3.13).
        """
        import posixpath

        import fs_organizer.config as config_mod

        home = tmp_path / "home"
        (home / "Downloads" / "invoices").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        # Run the POSIX expander on this platform, exactly as Linux does.
        monkeypatch.setattr(config_mod.os.path, "expanduser", posixpath.expanduser)

        src = home / "Downloads" / "invoices" / "a.pdf"
        src.write_text("x")
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("~\\Downloads\\invoices\\**", [".pdf"], "Invoices")],
        )
        assert cfg.category_for(src) == "Invoices"

    def test_bare_tilde_filename_is_not_treated_as_a_username(self, tmp_path, monkeypatch):
        """``~scan.pdf`` is a file-NAME glob, never a home reference.

        ``os.path.expanduser("~scan.pdf")`` returns ``C:\\Users\\scan.pdf`` on
        Windows -- CPython parses everything after a lone ``~`` as a
        username. Expanding it would silently rewrite a legal rule, so only a
        bare ``~`` or ``~`` + separator may expand.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        cfg = make_config(
            tmp_path,
            rules={".pdf": "Documents"},
            sub_rules=[_subrule("~scan.pdf", [".pdf"], "Scans")],
        )
        assert cfg.category_for(tmp_path / "watch" / "~scan.pdf") == "Scans"

    def test_tilde_user_syntax_is_not_expanded(self, tmp_path, monkeypatch):
        """``~other/...`` stays literal; it must not resolve to some other home."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        rule = SubRule.from_dict(_subrule("~someoneelse/x/**", [".pdf"], "Nope"), 0)
        assert rule.match_pattern() == "~someoneelse/x/**"

    def test_pattern_is_preserved_verbatim_for_export(self, tmp_path, monkeypatch):
        """to_dict() must round-trip the user's text, not an absolute path.

        Expanding into the stored pattern would bake the home directory into
        ``check --json`` and into any config written back from a dump.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        rule = SubRule.from_dict(_subrule("~/Downloads/**", [".pdf"], "Invoices"), 0)
        assert rule.pattern == "~/Downloads/**"
        assert rule.to_dict()["pattern"] == "~/Downloads/**"
        # ...while the match form is expanded and separator-normalized.
        # (expanduser keeps the backslash from a Windows HOME value; the
        # forward-slash normalization in match_pattern() is what makes the
        # two comparable.)
        normalized_home = str(tmp_path / "home").replace("\\", "/")
        assert rule.match_pattern() == normalized_home + "/Downloads/**"

    def test_backslashes_are_normalized_for_matching(self, tmp_path):
        """A Windows-style path glob must match the posix path form."""
        rule = SubRule.from_dict(_subrule("**\\invoices\\**", [".pdf"], "Invoices"), 0)
        assert rule.match_pattern() == "**/invoices/**"

    def test_plain_patterns_are_untouched(self, tmp_path):
        for pattern in ["**/invoices/**", "*.pdf", "x/**", "Documents/2026/*"]:
            rule = SubRule.from_dict(_subrule(pattern, [".pdf"], "C"), 0)
            assert rule.match_pattern() == pattern
