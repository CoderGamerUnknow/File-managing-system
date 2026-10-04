"""Tests for rule-based extension matching and ignore patterns."""
import os
from pathlib import Path

from fs_organizer.rules import is_ignored, is_under, match_extension, normalize_path_key


def test_is_under_self_child_and_sibling(tmp_path):
    root = normalize_path_key(tmp_path)
    child = normalize_path_key(tmp_path / "a" / "b.txt")
    sibling = normalize_path_key(tmp_path.parent / "elsewhere" / "b.txt")
    assert is_under(root, root)
    assert is_under(child, root)
    assert not is_under(sibling, root)
    # A sibling whose name merely STARTS with the root's name is not inside.
    almost = normalize_path_key(Path(str(tmp_path) + "-other") / "x")
    assert not is_under(almost, root)


def test_is_under_tolerates_root_already_ending_in_separator(tmp_path):
    """Appending os.sep to a root that already ends in one builds a prefix
    ('//' or 'c:\\\\') that no child path can match — which would make the
    watcher ignore every event (and the loop guard miss its own output) when
    the watched/target root IS a filesystem or drive root."""
    # Filesystem root itself: '/' on POSIX, '\\' on Windows.
    fs_root = normalize_path_key(Path(os.sep))
    child = normalize_path_key(Path(os.sep).joinpath("tmp", "x.txt"))
    assert fs_root.endswith(os.sep)
    assert is_under(child, fs_root)

    # A root spelled with a trailing separator behaves like the bare root.
    bare = normalize_path_key(tmp_path)
    file_key = normalize_path_key(tmp_path / "a.txt")
    assert is_under(file_key, bare + os.sep)

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
