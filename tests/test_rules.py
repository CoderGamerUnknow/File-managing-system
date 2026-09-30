"""Tests for rule-based extension matching and ignore patterns."""
from pathlib import Path

from fs_organizer.rules import is_ignored, match_extension


def test_match_simple_extension(tmp_path):
    rules = {".txt": "Documents", ".png": "Images"}
    assert match_extension(tmp_path / "notes.txt", rules) == "Documents"
    assert match_extension(tmp_path / "photo.png", rules) == "Images"


def test_match_case_insensitive(tmp_path):
    rules = {".txt": "Documents"}
    assert match_extension(tmp_path / "NOTES.TXT", rules) == "Documents"
    assert match_extension(tmp_path / "Report.Pdf", {".pdf": "Documents"}) == "Documents"


def test_no_match_returns_none(tmp_path):
    rules = {".txt": "Documents"}
    assert match_extension(tmp_path / "archive.rar", rules) is None


def test_file_without_extension(tmp_path):
    assert match_extension(tmp_path / "Makefile", {".txt": "Docs"}) is None


def test_hidden_dotfile_has_no_suffix(tmp_path):
    # ".gitignore" is a name, not an extension
    assert match_extension(tmp_path / ".gitignore", {".gitignore": "Nope"}) is None


def test_ignore_by_name(tmp_path):
    patterns = ["*.tmp", "Thumbs.db"]
    assert is_ignored(tmp_path / "song.mp3.tmp", patterns) is True
    assert is_ignored(tmp_path / "Thumbs.db", patterns) is True


def test_ignore_by_full_path(tmp_path):
    patterns = ["**/Downloads/**"]
    p = Path("/home/user/Downloads/setup.exe")
    assert is_ignored(p, patterns) is True


def test_not_ignored(tmp_path):
    patterns = ["*.tmp", ".*"]
    assert is_ignored(tmp_path / "important.docx", patterns) is False


def test_dotfiles_ignored(tmp_path):
    assert is_ignored(tmp_path / ".gitignore", [".*"]) is True
    assert is_ignored(tmp_path / ".hidden", [".*"]) is True
