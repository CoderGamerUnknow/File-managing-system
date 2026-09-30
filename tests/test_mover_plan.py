"""Tests for the mover.plan() dry-run pre-scan.

plan() is a pure, no-side-effect pre-scan: it reports every file that would
be organized, with its destination and size, without touching the
filesystem. It is the contract for the dashboard's preview capability.
"""

from fs_organizer.mover import plan
from fs_organizer.views import plan_summary, _fmt_size

from helpers import make_config as _make_config


def make_config(tmp_path, **overrides):
    """File-local default: this suite's fixtures also map .png -> Images."""
    return _make_config(
        tmp_path, rules={".txt": "Documents", ".png": "Images"}, **overrides
    )


def _write(root, *rel_parts, text="x"):
    p = root.joinpath(*rel_parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _out(root):
    return root / "out"


def _docs(root):
    return _out(root) / "Documents"


class TestPlan:
    def test_lists_only_matching_extensions(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        (watch / "a.txt").write_text("1", encoding="utf-8")
        (watch / "b.png").write_bytes(b"00")
        (watch / "orphan.dat").write_text("2", encoding="utf-8")
        (watch / "keep.tmp").write_text("x", encoding="utf-8")  # ignored

        # a.txt -> Documents; b.png -> Images; orphan.dat -> no rule; keep.tmp
        # -> ignored. Only the first two are organised.
        data = plan(cfg, max_files=100)

        names = {r["name"] for r in data["rows"]}
        assert names == {"a.txt", "b.png"}, names
        assert data["total"] == 2
        assert data["truncated"] is False
        assert data["would_move"] == 2

    def test_respects_ignored_and_unknown_extensions(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "keep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "watch" / "z.tmp").write_text("z", encoding="utf-8")
        (tmp_path / "watch" / "mystery.dat").write_text("d", encoding="utf-8")

        data = plan(cfg, max_files=100)
        names = {r["name"] for r in data["rows"]}
        assert names == {"keep.txt"}, names

    def test_truncated_when_over_cap(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch").mkdir(exist_ok=True)
        for i in range(5):
            (tmp_path / "watch" / f"f{i}.txt").write_text(str(i), encoding="utf-8")

        data = plan(cfg, max_files=2)
        assert data["total"] == 2
        assert data["truncated"] is True

    def test_empty_root(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch").mkdir(exist_ok=True)
        data = plan(cfg)
        assert data["total"] == 0 and data["rows"] == []

    def test_destination_matches_move_file(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        (watch / "a.txt").write_text("x", encoding="utf-8")

        planned = plan(cfg, max_files=10)
        assert planned["rows"] == [
            {
                "name": "a.txt",
                "category": "Documents",
                "destination": tmp_path / "out" / "Documents" / "a.txt",
                "size": 1,
                "would_move": False,
            }
        ]


class TestPlanSummary:
    def test_summary_covers_plan(self, tmp_path):
        cfg = make_config(tmp_path)
        watch = tmp_path / "watch"
        (watch / "a.txt").write_text("1", encoding="utf-8")
        (tmp_path / "out" / "Documents").mkdir(parents=True, exist_ok=True)

        summary = plan_summary(cfg)
        assert summary["would_move"] == 1
        assert summary["file_count"] == 1
        assert summary["bytes"] == 1
        assert summary["bytes_human"] == _fmt_size(1)


def _fmt_size(bytes_value: float) -> str:
    """Human readable size for the dashboard status card (mirror of ui.py)."""
    if not bytes_value >= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    value, unit_index = bytes_value, 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.1f} {units[unit_index]}"
