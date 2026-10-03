"""Tests for the V3 feature batch.

Covers: name rules, quiet hours, FSORG_* env overrides, the AI decision
cache, undo, rule suggestions, duplicate quarantine, journal export, the
disk-space guard, staged-move integrity verification, and the new CLI
subcommands (init / undo / suggest / export / quarantine).
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from fs_organizer import ai_cache, journal
from fs_organizer.__main__ import main
from fs_organizer.ai import classify_with_ai
from fs_organizer.config import (
    AIConfig,
    ConfigError,
    JournalConfig,
    NameRule,
    QuietHours,
    from_dict,
    load_config,
)
from fs_organizer.duplicates import find_duplicates, quarantine_duplicates
from fs_organizer.journal import export_journal
from fs_organizer.mover import _disk_space_ok, _free_percent, _staged_move, _verify_copy, move_file
from fs_organizer.suggest import render_suggestions, suggestions
from fs_organizer.undo import plan_undo, render_undo, undo
from fs_organizer.watcher import Organizer
from helpers import make_config

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _journal_cfg(tmp_path, **overrides):
    """Config with the journal enabled and pointed inside tmp_path."""
    overrides.setdefault("journal", JournalConfig(enabled=True, path=str(tmp_path / "moves.jsonl")))
    return make_config(tmp_path, **overrides)


def _write_journal_entry(cfg, src, dest, ts, category="Documents", size=5):
    path = cfg.journal.resolved_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": ts, "src": str(src), "dest": str(dest), "category": category, "size": size}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _now_minutes() -> int:
    now = datetime.now()
    return now.hour * 60 + now.minute


class _Usage:
    def __init__(self, total, used, free):
        self.total, self.used, self.free = total, used, free


# --------------------------------------------------------------------------
# Name rules
# --------------------------------------------------------------------------

class TestNameRuleParsing:
    @pytest.mark.parametrize("bad", [
        {},  # missing everything
        {"pattern": "invoice*"},  # no category
        {"category": "Invoices"},  # no pattern
        {"pattern": "   ", "category": "Invoices"},  # blank pattern
        {"pattern": "x", "category": "  "},  # blank category
        {"pattern": "[bad(", "category": "X", "regex": True},  # invalid regex
        {"pattern": "x", "category": "X", "extensions": ["pdf"]},  # missing dot
        {"pattern": "x", "category": "X", "extensions": ".pdf"},  # not a list
        "not-an-object",  # not a dict
    ])
    def test_invalid_name_rules_rejected(self, bad, tmp_path):
        with pytest.raises(ConfigError):
            from_dict({"watch_folders": [str(tmp_path)], "name_rules": [bad]})

    def test_valid_rule_parses_and_normalizes(self, tmp_path):
        cfg = from_dict({"watch_folders": [str(tmp_path)], "name_rules": [
            {"pattern": " Invoice* ", "category": " Invoices ", "regex": False,
             "extensions": [".PDF"]},
        ]})
        assert len(cfg.name_rules) == 1
        rule = cfg.name_rules[0]
        assert rule.pattern == "Invoice*"
        assert rule.category == "Invoices"
        assert rule.extensions == [".pdf"]

    def test_name_rules_must_be_a_list(self, tmp_path):
        with pytest.raises(ConfigError):
            from_dict({"watch_folders": [str(tmp_path)],
                       "name_rules": {"pattern": "x", "category": "Y"}})

    def test_round_trip_through_to_dict(self, tmp_path):
        cfg = from_dict({"watch_folders": [str(tmp_path)],
                         "name_rules": [{"pattern": "^IMG_", "category": "Photos", "regex": True}]})
        assert cfg.to_dict()["name_rules"] == [
            {"pattern": "^IMG_", "category": "Photos", "regex": True, "extensions": []}
        ]

    def test_defaults_to_empty(self, tmp_path):
        assert from_dict({"watch_folders": [str(tmp_path)]}).name_rules == []


class TestNameRuleMatching:
    def test_glob_match(self):
        rule = NameRule(pattern="invoice*", category="Invoices")
        assert rule.matches(Path("invoice_2024.pdf"))
        assert rule.matches(Path("/anywhere/invoice.pdf"))
        assert not rule.matches(Path("receipt.pdf"))

    def test_regex_match(self):
        rule = NameRule(pattern=r"^IMG_\d+", category="Photos", regex=True)
        assert rule.matches(Path("IMG_1234.jpg"))
        assert not rule.matches(Path("DSC_0001.jpg"))

    def test_extension_narrows_the_rule(self):
        rule = NameRule(pattern="report*", category="Reports", extensions=[".pdf"])
        assert rule.matches(Path("report_q3.pdf"))
        assert not rule.matches(Path("report_q3.txt"))

    def test_case_insensitive_extension_filter(self):
        rule = NameRule(pattern="*", category="Everything", extensions=[".TXT"])
        assert rule.matches(Path("any.TXT"))


class TestCategoryForPrecedence:
    def test_name_rule_before_extension_table(self, tmp_path):
        cfg = make_config(tmp_path, name_rules=[NameRule(pattern="report*", category="Reports")])
        # .log is not in the table; the name rule catches it first anyway.
        assert cfg.category_for(tmp_path / "watch" / "report_q3.log") == "Reports"
        # A name the rule does not match falls through to the table.
        assert cfg.category_for(tmp_path / "watch" / "notes.txt") == "Documents"

    def test_sub_rule_beats_name_rule(self, tmp_path):
        sub = {"pattern": (tmp_path / "watch").as_posix() + "/**",
               "extensions": [".txt"], "category": "FromFolder"}
        cfg = make_config(
            tmp_path,
            sub_rules=[sub],
            name_rules=[NameRule(pattern="*", category="ByName")],
        )
        assert cfg.category_for(tmp_path / "watch" / "a.txt") == "FromFolder"
        # .pdf: sub-rule does not apply (extension), name rule catches it.
        assert cfg.category_for(tmp_path / "watch" / "a.pdf") == "ByName"

    def test_table_fallback_when_nothing_else_matches(self, tmp_path):
        cfg = make_config(tmp_path)  # only {.txt: Documents}
        assert cfg.category_for(tmp_path / "watch" / "a.txt") == "Documents"
        assert cfg.category_for(tmp_path / "watch" / "a.xyz") is None


# --------------------------------------------------------------------------
# Quiet hours
# --------------------------------------------------------------------------

class TestQuietHours:
    def test_disabled_by_default_always_allows(self):
        qh = QuietHours()
        assert not qh.enabled
        assert qh.allows(datetime(2026, 1, 1, 3, 0))

    def test_inside_normal_window(self):
        qh = QuietHours(start="08:00", end="22:00")
        assert qh.enabled
        assert qh.allows(datetime(2026, 1, 15, 8, 0))  # boundary start
        assert qh.allows(datetime(2026, 1, 15, 12, 0))
        assert qh.allows(datetime(2026, 1, 15, 22, 0))  # boundary end

    def test_outside_normal_window(self):
        qh = QuietHours(start="08:00", end="22:00")
        assert not qh.allows(datetime(2026, 1, 15, 7, 59))
        assert not qh.allows(datetime(2026, 1, 15, 22, 1))

    def test_window_wraps_midnight(self):
        qh = QuietHours(start="22:00", end="06:00")
        assert qh.allows(datetime(2026, 1, 15, 23, 30))
        assert qh.allows(datetime(2026, 1, 15, 3, 0))
        assert not qh.allows(datetime(2026, 1, 15, 12, 0))
        assert not qh.allows(datetime(2026, 1, 15, 6, 1))
        assert not qh.allows(datetime(2026, 1, 15, 21, 59))

    def test_from_dict_valid(self):
        qh = QuietHours.from_dict({"start": "08:00", "end": "22:00"})
        assert qh.to_dict() == {"start": "08:00", "end": "22:00"}

    def test_from_dict_empty_object_disables(self):
        qh = QuietHours.from_dict({})
        assert not qh.enabled
        assert qh.allows(datetime(2026, 1, 1, 3, 0))

    @pytest.mark.parametrize("bad", [
        {"start": "08:00"},  # missing end
        {"end": "22:00"},  # missing start
        {"start": "25:00", "end": "06:00"},  # hour out of range
        {"start": "08:60", "end": "22:00"},  # minute out of range
        {"start": "8am", "end": "10pm"},  # not HH:MM
        ["08:00", "22:00"],  # not an object
    ])
    def test_from_dict_invalid(self, bad):
        with pytest.raises(ConfigError):
            QuietHours.from_dict(bad)

    def test_config_wiring(self, tmp_path):
        cfg = from_dict({"watch_folders": [str(tmp_path)],
                         "quiet_hours": {"start": "22:00", "end": "06:00"}})
        assert cfg.quiet_hours.enabled
        assert cfg.to_dict()["quiet_hours"] == {"start": "22:00", "end": "06:00"}

    def test_organizer_acts_inside_window(self, tmp_path):
        cfg = make_config(tmp_path)
        start, end = _hhmm(_now_minutes() - 60), _hhmm(_now_minutes() + 60)
        cfg.quiet_hours = QuietHours(start=start, end=end)
        path = tmp_path / "watch" / "a.txt"
        path.write_text("x", encoding="utf-8")
        Organizer(cfg).handle(path)
        assert (tmp_path / "out" / "Documents" / "a.txt").exists()

    def test_organizer_defers_outside_window(self, tmp_path):
        cfg = make_config(tmp_path)
        start, end = _hhmm(_now_minutes() + 60), _hhmm(_now_minutes() + 120)
        cfg.quiet_hours = QuietHours(start=start, end=end)
        path = tmp_path / "watch" / "a.txt"
        path.write_text("x", encoding="utf-8")
        Organizer(cfg).handle(path)
        assert path.exists()  # left in place, not moved
        assert not (tmp_path / "out" / "Documents" / "a.txt").exists()


# --------------------------------------------------------------------------
# FSORG_* environment overrides
# --------------------------------------------------------------------------

class TestEnvOverrides:
    def _write(self, tmp_path, dry_run, target_root=None):
        (tmp_path / "watch").mkdir(exist_ok=True)
        data = {
            "watch_folders": [str(tmp_path / "watch")],
            "target_rules": {".txt": "Documents"},
            "target_root": target_root or str(tmp_path / "out"),
            "dry_run": dry_run,
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_dry_run_forced_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FSORG_DRY_RUN", "true")
        assert load_config(self._write(tmp_path, dry_run=False)).dry_run is True

    def test_dry_run_forced_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FSORG_DRY_RUN", "0")
        assert load_config(self._write(tmp_path, dry_run=True)).dry_run is False

    def test_target_root_overridden(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FSORG_TARGET_ROOT", str(tmp_path / "elsewhere"))
        cfg = load_config(self._write(tmp_path, dry_run=False))
        assert cfg.target_root == str(tmp_path / "elsewhere")

    def test_blank_target_root_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FSORG_TARGET_ROOT", "   ")
        cfg = load_config(self._write(tmp_path, dry_run=False))
        assert cfg.target_root == str(tmp_path / "out")

    def test_unset_keeps_file_values(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FSORG_DRY_RUN", raising=False)
        monkeypatch.delenv("FSORG_TARGET_ROOT", raising=False)
        cfg = load_config(self._write(tmp_path, dry_run=True))
        assert cfg.dry_run is True
        assert cfg.target_root == str(tmp_path / "out")


# --------------------------------------------------------------------------
# AI decision cache
# --------------------------------------------------------------------------

class TestAiCache:
    def test_cache_key_stable_and_content_sensitive(self, tmp_path):
        cfg = AIConfig(provider="ollama", model="llama3.2")
        a = tmp_path / "a.xyz"
        a.write_text("same content", encoding="utf-8")
        b = tmp_path / "b.xyz"
        b.write_text("same content", encoding="utf-8")
        c = tmp_path / "c.xyz"
        c.write_text("different", encoding="utf-8")
        assert ai_cache.cache_key(a, cfg) == ai_cache.cache_key(b, cfg)
        assert ai_cache.cache_key(a, cfg) != ai_cache.cache_key(c, cfg)

    def test_cache_key_depends_on_model(self, tmp_path):
        p = tmp_path / "a.xyz"
        p.write_text("content", encoding="utf-8")
        k1 = ai_cache.cache_key(p, AIConfig(model="llama3.2"))
        k2 = ai_cache.cache_key(p, AIConfig(model="mistral"))
        assert k1 != k2

    def test_cache_key_none_for_missing_file(self, tmp_path):
        assert ai_cache.cache_key(tmp_path / "ghost.xyz", AIConfig()) is None

    def test_load_missing_or_corrupt_is_empty(self, tmp_path):
        assert ai_cache.load(tmp_path / "nope.json") == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert ai_cache.load(bad) == {}
        not_dict = tmp_path / "list.json"
        not_dict.write_text("[1, 2]", encoding="utf-8")
        assert ai_cache.load(not_dict) == {}

    def test_save_load_round_trip(self, tmp_path):
        path = tmp_path / "sub" / "cache.json"
        ai_cache.save(path, {"k": "Music"})
        assert ai_cache.load(path) == {"k": "Music"}

    def test_put_semantics(self):
        cache: dict = {}
        assert ai_cache.put(cache, None, "Music") is False  # no key
        assert ai_cache.put(cache, "k", "") is False  # no category
        assert ai_cache.put(cache, "k", "Music") is True
        assert ai_cache.put(cache, "k", "Music") is False  # unchanged
        assert ai_cache.get(cache, "k") == "Music"
        assert ai_cache.get(cache, None) is None

    @staticmethod
    def _fake_response(payload, status=200):
        import io

        body = io.BytesIO(json.dumps(payload).encode("utf-8"))

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.close()
                return False

            def getcode(self):
                return status

        return FakeResp(body.read())

    def test_classify_hits_cache_without_network(self, tmp_path, monkeypatch):
        from fs_organizer import ai

        def fail(*a, **k):
            raise AssertionError("network must not be touched on a cache hit")

        monkeypatch.setattr(ai.urllib.request, "urlopen", fail)
        cfg = AIConfig(
            enabled=True, provider="ollama", model="llama3.2",
            allowed_subfolders=["Music"], extensions=[".xyz"],
            cache_enabled=True, cache_path=str(tmp_path / "cache.json"),
        )
        p = tmp_path / "f.xyz"
        p.write_text("payload", encoding="utf-8")
        ai_cache.save(Path(cfg.cache_path), {ai_cache.cache_key(p, cfg): "Music"})
        assert classify_with_ai(p, cfg) == "Music"

    def test_classify_populates_cache_on_success(self, tmp_path, monkeypatch):
        from fs_organizer import ai

        def fake_urlopen(req, timeout=None):
            return self._fake_response({"message": {"content": '{"category": "Music"}'}})

        monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
        cfg = AIConfig(
            enabled=True, provider="ollama", model="llama3.2",
            allowed_subfolders=["Music"], extensions=[".xyz"],
            cache_enabled=True, cache_path=str(tmp_path / "cache.json"),
        )
        p = tmp_path / "f.xyz"
        p.write_text("payload", encoding="utf-8")
        assert classify_with_ai(p, cfg) == "Music"
        stored = ai_cache.load(Path(cfg.cache_path))
        assert stored.get(ai_cache.cache_key(p, cfg)) == "Music"

    def test_failed_classification_is_not_cached(self, tmp_path, monkeypatch):
        from fs_organizer import ai

        def fake_urlopen(req, timeout=None):
            return self._fake_response({"message": {"content": "not json"}})

        monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
        cache_path = tmp_path / "cache.json"
        cfg = AIConfig(
            enabled=True, provider="ollama", model="llama3.2",
            allowed_subfolders=["Music"], extensions=[".xyz"],
            cache_enabled=True, cache_path=str(cache_path),
        )
        p = tmp_path / "f.xyz"
        p.write_text("payload", encoding="utf-8")
        assert classify_with_ai(p, cfg) is None
        assert ai_cache.load(cache_path) == {}

    def test_cache_disabled_never_writes(self, tmp_path):
        cfg = AIConfig(
            enabled=True, extensions=[".xyz"], cache_enabled=False,
            cache_path=str(tmp_path / "cache.json"),
        )
        p = tmp_path / "f.xyz"
        p.write_text("x", encoding="utf-8")
        # Cache disabled: classify (network will fail here, but that is fine)
        # must never create the cache file.
        classify_with_ai(p, cfg)
        assert not (tmp_path / "cache.json").exists()


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

class TestUndo:
    def test_restores_moved_file(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        _write_journal_entry(cfg, src, dest, ts=100)

        results = undo(cfg)
        assert len(results) == 1
        assert results[0].moved
        assert results[0].restored_to == src
        assert src.exists() and src.read_text(encoding="utf-8") == "payload"
        assert not dest.exists()

    def test_never_overwrites_reoccupied_original(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("new occupant", encoding="utf-8")
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("organized", encoding="utf-8")
        _write_journal_entry(cfg, src, dest, ts=100)

        results = undo(cfg)
        assert results[0].moved
        restored = results[0].restored_to
        assert restored != src  # suffix, not overwrite
        assert restored.name == "a (1).txt"
        assert src.read_text(encoding="utf-8") == "new occupant"
        assert restored.read_text(encoding="utf-8") == "organized"

    def test_missing_destination_is_skipped(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        dest = tmp_path / "out" / "Documents" / "a.txt"
        _write_journal_entry(cfg, src, dest, ts=100)

        results = undo(cfg)
        assert results[0].skipped
        assert "no longer exists" in results[0].reason
        assert not src.exists()

    def test_dry_run_moves_nothing(self, tmp_path):
        cfg = _journal_cfg(tmp_path, dry_run=True)
        src = tmp_path / "watch" / "a.txt"
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        _write_journal_entry(cfg, src, dest, ts=100)

        results = undo(cfg)
        assert results[0].would_move
        assert dest.exists()
        assert not src.exists()

    def test_count_bounds_the_batch_to_newest(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        older = tmp_path / "out" / "Documents" / "a.txt"
        newer = tmp_path / "out" / "Documents" / "b.txt"
        older.parent.mkdir(parents=True)
        older.write_text("old", encoding="utf-8")
        newer.write_text("new", encoding="utf-8")
        _write_journal_entry(cfg, tmp_path / "watch" / "a.txt", older, ts=100)
        _write_journal_entry(cfg, tmp_path / "watch" / "b.txt", newer, ts=200)

        results = undo(cfg, count=1)
        assert len(results) == 1
        assert newer is not None and not newer.exists()  # newest restored
        assert older.exists()  # older left alone

    def test_plan_undo_is_read_only(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        _write_journal_entry(cfg, tmp_path / "watch" / "a.txt", dest, ts=100)

        results = plan_undo(cfg)
        assert results[0].would_move
        assert dest.exists()  # nothing actually happened

    def test_empty_journal_renders_cleanly(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        assert "nothing to undo" in render_undo(undo(cfg))

    def test_render_lists_outcomes(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        _write_journal_entry(cfg, tmp_path / "watch" / "a.txt", dest, ts=100)
        text = render_undo(undo(cfg))
        assert "restored" in text


# --------------------------------------------------------------------------
# Rule suggestions
# --------------------------------------------------------------------------

class TestSuggestions:
    def test_unknown_extensions_are_suggested(self, tmp_path):
        cfg = make_config(tmp_path)  # only {.txt: Documents}, AI off
        watch = tmp_path / "watch"
        (watch / "a.log").write_text("x", encoding="utf-8")
        (watch / "b.log").write_text("y", encoding="utf-8")
        (watch / "c.xyz").write_text("z", encoding="utf-8")
        (watch / "covered.txt").write_text("ok", encoding="utf-8")

        data = suggestions(cfg)
        assert data["skipped_unknown_total"] == 3
        exts = [s["extension"] for s in data["suggested_rules"]]
        assert exts == [".log", ".xyz"]  # most frequent first
        assert data["suggested_rules"][0]["count"] == 2
        assert data["suggested_rules"][0]["snippet"] == {".log": "Other"}
        assert data["suggested_rules"][0]["example"] in {"a.log", "b.log"}

    def test_covered_files_are_not_suggested(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("x", encoding="utf-8")
        data = suggestions(cfg)
        assert data["skipped_unknown_total"] == 0
        assert data["suggested_rules"] == []

    def test_journal_activity_by_category(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        _write_journal_entry(cfg, tmp_path / "s.txt", tmp_path / "d.txt", ts=100,
                             category="Documents")
        _write_journal_entry(cfg, tmp_path / "s2.txt", tmp_path / "d2.txt", ts=101,
                             category="Documents")
        _write_journal_entry(cfg, tmp_path / "m.mp3", tmp_path / "d3.mp3", ts=102,
                             category="Music")
        data = suggestions(cfg)
        assert data["journal_moves_by_category"] == {"Documents": 2, "Music": 1}
        assert data["journal_enabled"] is True

    def test_unused_categories_reported(self, tmp_path):
        cfg = make_config(tmp_path, ai=AIConfig(enabled=False, allowed_subfolders=["Music", "Videos"]))
        data = suggestions(cfg)
        # Neither "Music" nor "Videos" is a rule category nor in the journal.
        assert data["unused_categories"] == ["Music", "Videos"]

    def test_render_output(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.log").write_text("x", encoding="utf-8")
        text = render_suggestions(suggestions(cfg))
        assert "match no rule" in text
        assert ".log" in text

    def test_render_when_everything_covered(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("x", encoding="utf-8")
        assert "no unmatched files found" in render_suggestions(suggestions(cfg))


# --------------------------------------------------------------------------
# Duplicate quarantine
# --------------------------------------------------------------------------

class TestQuarantine:
    def _dupe_cfg(self, tmp_path):
        cfg = make_config(tmp_path, ignore_patterns=[])
        watch = tmp_path / "watch"
        older = watch / "older.bin"
        newer = watch / "newer.bin"
        (watch / "unique.bin").write_text("unique", encoding="utf-8")
        older.write_text("duplicate payload", encoding="utf-8")
        newer.write_text("duplicate payload", encoding="utf-8")
        # Pin mtimes so the oldest-stays order is deterministic.
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        return cfg, older, newer

    def test_second_copy_is_quarantined_oldest_stays(self, tmp_path):
        cfg, older, newer = self._dupe_cfg(tmp_path)
        report = find_duplicates(cfg)
        root = tmp_path / "quar"
        data = quarantine_duplicates(cfg, report, root, dry_run=False)

        assert data["moved_count"] == 1
        assert older.exists()  # original untouched
        assert not newer.exists()  # duplicate moved aside
        moved = Path(data["moved"][0]["dest"])
        assert moved.exists()
        assert root in moved.parents
        assert moved.read_text(encoding="utf-8") == "duplicate payload"
        assert not data["skipped"]

    def test_dry_run_lists_without_moving(self, tmp_path):
        cfg, older, newer = self._dupe_cfg(tmp_path)
        report = find_duplicates(cfg)
        data = quarantine_duplicates(cfg, report, tmp_path / "quar", dry_run=True)
        assert data["dry_run"] is True
        assert data["moved_count"] == 1
        assert older.exists() and newer.exists()  # nothing moved

    def test_nothing_is_ever_deleted(self, tmp_path):
        cfg, older, _newer = self._dupe_cfg(tmp_path)
        report = find_duplicates(cfg)
        quarantine_duplicates(cfg, report, tmp_path / "quar", dry_run=False)
        # Both byte-identical copies still exist somewhere on disk.
        contents = [
            *sorted(
                p.read_text(encoding="utf-8")
                for p in (tmp_path / "quar").rglob("*") if p.is_file()
            ),
            older.read_text(encoding="utf-8"),
        ]
        assert contents == ["duplicate payload", "duplicate payload"]

    def test_vanished_file_is_skipped_not_fatal(self, tmp_path):
        cfg, _older, newer = self._dupe_cfg(tmp_path)
        report = find_duplicates(cfg)
        newer.unlink()  # disappears between scan and quarantine
        data = quarantine_duplicates(cfg, report, tmp_path / "quar", dry_run=False)
        assert data["moved_count"] == 0
        assert len(data["skipped"]) == 1
        assert "no longer exists" in data["skipped"][0]["reason"]


# --------------------------------------------------------------------------
# Journal export
# --------------------------------------------------------------------------

class TestExportJournal:
    def test_csv_contains_header_and_rows(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        _write_journal_entry(cfg, tmp_path / "s.txt", tmp_path / "d.txt", ts=100)
        _write_journal_entry(cfg, tmp_path / "s2.txt", tmp_path / "d2.txt", ts=101)
        out = export_journal(cfg, fmt="csv")
        lines = out.strip().split("\n")
        assert lines[0] == "ts,ts_iso,src,dest,category,size"
        assert len(lines) == 3
        assert str(tmp_path / "s2.txt") in out

    def test_json_is_a_parseable_list(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        _write_journal_entry(cfg, tmp_path / "s.txt", tmp_path / "d.txt", ts=100)
        rows = json.loads(export_journal(cfg, fmt="json"))
        assert len(rows) == 1
        assert rows[0]["ts"] == 100
        assert rows[0]["category"] == "Documents"
        assert rows[0]["ts_iso"]  # human-readable timestamp alongside epoch

    def test_disabled_journal_exports_empty_documents(self, tmp_path):
        cfg = make_config(tmp_path)  # journal disabled by default
        assert export_journal(cfg, fmt="csv").strip().split("\n") == [
            "ts,ts_iso,src,dest,category,size"
        ]
        assert json.loads(export_journal(cfg, fmt="json")) == []

    def test_missing_journal_file_exports_empty_documents(self, tmp_path):
        cfg = _journal_cfg(tmp_path)
        assert json.loads(export_journal(cfg, fmt="json")) == []
        assert "ts,ts_iso" in export_journal(cfg, fmt="csv")


# --------------------------------------------------------------------------
# Disk-space guard
# --------------------------------------------------------------------------

class TestDiskSpaceGuard:
    def _patch_usage(self, monkeypatch, total=100, used=90, free=10):
        from fs_organizer import mover

        monkeypatch.setattr(mover.shutil, "disk_usage",
                            lambda p: _Usage(total, used, free))

    def test_disabled_guard_always_ok(self, tmp_path, monkeypatch):
        cfg = make_config(tmp_path)
        assert cfg.disk_space_guard == 0.0
        assert _disk_space_ok(tmp_path, cfg) is True

    def test_guard_blocks_below_threshold(self, tmp_path, monkeypatch):
        self._patch_usage(monkeypatch, total=100, used=90, free=10)
        cfg = make_config(tmp_path, disk_space_guard=50.0)
        assert _disk_space_ok(tmp_path, cfg) is False
        cfg2 = make_config(tmp_path, disk_space_guard=5.0)
        assert _disk_space_ok(tmp_path, cfg2) is True

    def test_unqueryable_volume_never_blocks(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        def boom(p):
            raise OSError("no such volume")

        monkeypatch.setattr(mover.shutil, "disk_usage", boom)
        cfg = make_config(tmp_path, disk_space_guard=50.0)
        assert _disk_space_ok(tmp_path, cfg) is True  # fail open, not closed

    def test_free_percent_reflects_usage(self, tmp_path, monkeypatch):
        self._patch_usage(monkeypatch, total=1000, used=250, free=750)
        assert _free_percent(tmp_path) == pytest.approx(75.0)

    def test_move_file_refuses_when_volume_nearly_full(self, tmp_path, monkeypatch):
        self._patch_usage(monkeypatch, total=100, used=95, free=5)
        cfg = make_config(tmp_path, disk_space_guard=50.0)
        path = tmp_path / "watch" / "a.txt"
        path.write_text("payload", encoding="utf-8")

        result = move_file(path, "Documents", cfg)
        assert result.skipped
        assert "nearly full" in result.reason
        assert path.exists()  # source untouched
        assert not (tmp_path / "out" / "Documents" / "a.txt").exists()


# --------------------------------------------------------------------------
# Staged-move integrity verification
# --------------------------------------------------------------------------

class TestStagedMoveIntegrity:
    def test_identical_copy_verifies(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"same bytes")
        b.write_bytes(b"same bytes")
        assert _verify_copy(a, b) is True

    def test_same_size_different_content_fails(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"aaaa")
        b.write_bytes(b"bbbb")
        assert _verify_copy(a, b) is False

    def test_different_size_fails(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"aaa")
        b.write_bytes(b"aaaa")
        assert _verify_copy(a, b) is False

    def test_missing_file_fails(self, tmp_path):
        a = tmp_path / "a.bin"
        a.write_bytes(b"data")
        assert _verify_copy(a, tmp_path / "ghost.bin") is False

    def test_failed_verification_aborts_and_cleans_up(self, tmp_path, monkeypatch):
        from fs_organizer import mover

        monkeypatch.setattr(mover, "_verify_copy", lambda s, c: False)
        src = tmp_path / "src.bin"
        src.write_bytes(b"precious data")
        final = tmp_path / "dest.bin"

        with pytest.raises(OSError, match="integrity"):
            _staged_move(src, final)

        assert src.exists()  # source survives untouched
        assert not final.exists()  # nothing half-committed
        assert not (tmp_path / "dest.bin.part").exists()  # temp cleaned up


# --------------------------------------------------------------------------
# New CLI subcommands
# --------------------------------------------------------------------------

def _cli_cfg(tmp_path, **overrides):
    (tmp_path / "watch").mkdir(exist_ok=True)
    data = {
        "watch_folders": [str(tmp_path / "watch")],
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
    return p


class TestCliSubcommands:
    def test_init_writes_a_config(self, tmp_path, capsys):
        target = tmp_path / "new" / "config.json"
        rc = main([str(target), "init"])
        assert rc == 0
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["watch_folders"]
        assert payload["dry_run"] is True  # safe defaults
        assert payload["target_rules"]

    def test_init_refuses_overwrite_without_force(self, tmp_path, capsys):
        target = tmp_path / "config.json"
        assert main([str(target), "init"]) == 0
        assert main([str(target), "init"]) == 1
        assert "Refusing to overwrite" in capsys.readouterr().out

    def test_init_force_overwrites(self, tmp_path, capsys):
        target = tmp_path / "config.json"
        assert main([str(target), "init"]) == 0
        assert main([str(target), "init", "--force"]) == 0

    def test_init_honors_output_flag(self, tmp_path):
        target = tmp_path / "elsewhere.json"
        assert main([str(tmp_path / "dummy.json"), "init", "--output", str(target)]) == 0
        assert target.exists()

    def test_undo_with_journal_disabled_exits_1(self, tmp_path, capsys):
        p = _cli_cfg(tmp_path)  # journal defaults to disabled
        rc = main([str(p), "undo"])
        assert rc == 1
        assert "Journal is disabled" in capsys.readouterr().out

    def test_undo_round_trip_via_cli(self, tmp_path, capsys):
        journal_path = tmp_path / "moves.jsonl"
        p = _cli_cfg(tmp_path, journal={"enabled": True, "path": str(journal_path)})
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        journal.append_move(
            load_config(p), tmp_path / "watch" / "a.txt", dest, 7, "Documents"
        )
        rc = main([str(p), "undo", "1"])
        assert rc == 0
        assert (tmp_path / "watch" / "a.txt").read_text(encoding="utf-8") == "payload"
        assert not dest.exists()
        assert "restored" in capsys.readouterr().out

    def test_undo_dry_run_via_cli(self, tmp_path, capsys):
        journal_path = tmp_path / "moves.jsonl"
        p = _cli_cfg(tmp_path, journal={"enabled": True, "path": str(journal_path)})
        dest = tmp_path / "out" / "Documents" / "a.txt"
        dest.parent.mkdir(parents=True)
        dest.write_text("payload", encoding="utf-8")
        journal.append_move(load_config(p), tmp_path / "watch" / "a.txt", dest, 7, "Documents")
        rc = main([str(p), "undo", "1", "--dry-run"])
        assert rc == 0
        assert dest.exists()  # nothing moved

    def test_suggest_subcommand_runs(self, tmp_path, capsys):
        p = _cli_cfg(tmp_path)
        (tmp_path / "watch" / "a.log").write_text("x", encoding="utf-8")
        rc = main([str(p), "suggest"])
        assert rc == 0
        assert ".log" in capsys.readouterr().out

    def test_suggest_json_output(self, tmp_path, capsys):
        p = _cli_cfg(tmp_path)
        (tmp_path / "watch" / "a.log").write_text("x", encoding="utf-8")
        rc = main([str(p), "suggest", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["skipped_unknown_total"] == 1

    def test_export_csv_via_cli(self, tmp_path, capsys):
        journal_path = tmp_path / "moves.jsonl"
        p = _cli_cfg(tmp_path, journal={"enabled": True, "path": str(journal_path)})
        journal.append_move(
            load_config(p), tmp_path / "watch" / "a.txt",
            tmp_path / "out" / "Documents" / "a.txt", 7, "Documents",
        )
        rc = main([str(p), "export"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "ts,ts_iso,src,dest,category,size"
        assert len(out.strip().splitlines()) == 2

    def test_export_json_via_cli(self, tmp_path, capsys):
        p = _cli_cfg(tmp_path)  # disabled journal
        rc = main([str(p), "export", "--format", "json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_quarantine_dry_run_via_cli(self, tmp_path, capsys):
        p = _cli_cfg(tmp_path, ignore_patterns=[])
        watch = tmp_path / "watch"
        (watch / "a.bin").write_bytes(b"dup")
        (watch / "b.bin").write_bytes(b"dup")
        rc = main([str(p), "quarantine", "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "would quarantine" in out
        assert (watch / "a.bin").exists() and (watch / "b.bin").exists()
