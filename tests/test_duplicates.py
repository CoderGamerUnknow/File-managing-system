"""Tests for V2 duplicate detection (report-only, hash-based)."""
import hashlib

from fs_organizer.duplicates import find_duplicates, hash_file, render_duplicates
from helpers import make_config


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestHashFile:
    def test_matches_reference_sha256(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello world")
        assert hash_file(f) == _sha(b"hello world")

    def test_large_file_streamed_correctly(self, tmp_path):
        # Bigger than one 1 MiB chunk exercises the streaming loop.
        payload = b"x" * (1024 * 1024 + 123)
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        assert hash_file(f) == _sha(payload)


class TestFindDuplicates:
    def test_finds_identical_files(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("same")
        (tmp_path / "watch" / "b.txt").write_text("same")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"], "expected one duplicate group"
        group = data["duplicate_groups"][0]
        assert group["size"] == 4
        assert len(group["files"]) == 2
        assert data["duplicate_files"] == 2
        assert data["wasted_bytes"] == 4

    def test_unique_sizes_are_never_hashed(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("x")
        (tmp_path / "watch" / "bb.txt").write_text("yy")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"] == []
        assert data["files_hashed"] == 0  # unique sizes prove uniqueness
        assert data["bytes_hashed"] == 0

    def test_different_content_same_size_is_not_duplicate(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("aaa")
        (tmp_path / "watch" / "b.txt").write_text("bbb")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"] == []
        assert data["files_hashed"] == 2  # both needed hashing to prove it

    def test_ignores_ignored_patterns(self, tmp_path):
        cfg = make_config(tmp_path)  # helpers default: "*.tmp" ignored
        (tmp_path / "watch" / "a.tmp").write_text("same")
        (tmp_path / "watch" / "b.tmp").write_text("same")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"] == []
        assert data["scanned_files"] == 0

    def test_target_root_never_scanned(self, tmp_path):
        cfg = make_config(tmp_path)
        out = tmp_path / "out"
        out.mkdir()
        (out / "a.txt").write_text("same")
        (out / "b.txt").write_text("same")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"] == []
        assert data["scanned_files"] == 0

    def test_recursive_scope_follows_config(self, tmp_path):
        (tmp_path / "watch" / "sub").mkdir(parents=True)
        (tmp_path / "watch" / "sub" / "a.txt").write_text("same")
        (tmp_path / "watch" / "b.txt").write_text("same")

        top = find_duplicates(make_config(tmp_path))
        assert top["duplicate_groups"] == []

        deep = find_duplicates(make_config(tmp_path, recursive=True))
        assert len(deep["duplicate_groups"]) == 1

    def test_oldest_file_is_first_in_group(self, tmp_path):
        import os

        cfg = make_config(tmp_path)
        old = tmp_path / "watch" / "old.txt"
        new = tmp_path / "watch" / "new.txt"
        old.write_text("same")
        new.write_text("same")
        # Deterministically age the first file.
        past = 1_000_000_000
        os.utime(old, (past, past))
        data = find_duplicates(cfg)
        files = data["duplicate_groups"][0]["files"]
        assert files[0]["path"] == old
        assert files[1]["path"] == new

    def test_hashing_budget_stops_new_groups(self, tmp_path):
        cfg = make_config(tmp_path)
        for name in ("a1.txt", "a2.txt"):
            (tmp_path / "watch" / name).write_text("content-a")
        for name in ("b1.txt", "b2.txt"):
            (tmp_path / "watch" / name).write_text("content-b")
        data = find_duplicates(cfg, max_bytes_hashed=5)
        # Both pairs share sizes; the budget only covers one group's verify.
        assert data["truncated"] is True
        assert data["bytes_hashed"] <= 10

    def test_file_cap_truncates_walk(self, tmp_path):
        cfg = make_config(tmp_path)
        for i in range(10):
            (tmp_path / "watch" / f"f{i}.txt").write_text("same")
        data = find_duplicates(cfg, max_files=4)
        assert data["truncated"] is True
        assert data["scanned_files"] == 4

    def test_min_size_filters_tiny_files(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("ab")
        (tmp_path / "watch" / "b.txt").write_text("ab")
        data = find_duplicates(cfg, min_size=3)
        assert data["duplicate_groups"] == []

    def test_missing_watch_folder_is_tolerated(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.watch_folders = [str(tmp_path / "watch"), str(tmp_path / "gone")]
        data = find_duplicates(cfg)
        assert data["duplicate_groups"] == []
        assert str(tmp_path / "gone") in data["watch_folders"]

    def test_report_only_never_mutates(self, tmp_path):
        cfg = make_config(tmp_path)
        a = tmp_path / "watch" / "a.txt"
        b = tmp_path / "watch" / "b.txt"
        a.write_text("same")
        b.write_text("same")
        find_duplicates(cfg)
        assert a.is_file() and b.is_file()
        assert a.read_text() == "same" and b.read_text() == "same"
        assert not (tmp_path / "out").exists()

    def test_hash_field_is_sha256_of_content(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("same")
        (tmp_path / "watch" / "b.txt").write_text("same")
        data = find_duplicates(cfg)
        assert data["duplicate_groups"][0]["hash"] == _sha(b"same")


class TestRenderDuplicates:
    def test_renders_groups_and_disclaimer(self, tmp_path):
        cfg = make_config(tmp_path)
        (tmp_path / "watch" / "a.txt").write_text("same")
        (tmp_path / "watch" / "b.txt").write_text("same")
        text = render_duplicates(find_duplicates(cfg))
        assert "report only" in text
        assert str(tmp_path / "watch" / "a.txt") in text
        assert str(tmp_path / "watch" / "b.txt") in text

    def test_renders_empty_result(self, tmp_path):
        cfg = make_config(tmp_path)
        text = render_duplicates(find_duplicates(cfg))
        assert "no duplicates" in text
