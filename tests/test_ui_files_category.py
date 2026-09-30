"""Contract test for the ``category`` field of the files payload.

The dashboard must show the mover's source of truth — the first path
segment under the target root — rather than a date subfolder or an
implementation detail.

Cases covered (input -> expected category):
    (no date subfolders, file at root)           -> ""
    (no date subfolders, file in Documents/)     -> Documents
    (date subfolders, file in Documents/)        -> Documents   # bug fixed
    (date subfolders, file in root)              -> ""
"""
import pytest

from fs_organizer.config import Config
from fs_organizer.views import _file_category, files_payload


def _write(root, *rel_parts, text="x"):
    p = root.joinpath(*rel_parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _payload(tmp_path, use_date_subfolders):
    cfg = Config(
        watch_folders=[str(tmp_path / "watch")],
        target_rules={".txt": "Documents", ".png": "Images"},
        ignore_patterns=["*.tmp"],
        target_root=str(tmp_path / "out"),
        use_date_subfolders=use_date_subfolders,
    )
    return files_payload(cfg)


def _rows(data):
    return [f for files in data["groups"].values() for f in files]


def _by_name(rows):
    return {f["name"]: f for f in rows}


@pytest.mark.parametrize(
    "sets,expected,use_date_subfolders",
    [
        (("a.txt",), "", False),
        (("Documents", "a.txt"), "Documents", False),
        (("Documents", "2026-09", "a.txt"), "Documents", False),
        (("a.txt",), "", True),
        (("Documents", "a.txt"), "Documents", True),
        (("Documents", "2026-09", "a.txt"), "Documents", True),
    ],
    ids=[
        "root-file-no-subfolders",
        "category-folder-no-subfolders",
        "date-folder-no-subfolders",
        "root-file-date-subfolders",
        "category-folder-date-subfolders",
        "date-folder-date-subfolders",
    ],
)
def test_files_payload_category_uses_first_segment_under_root(
    tmp_path, sets, expected, use_date_subfolders
):
    """Contract: the category shown is the first path segment below the
    target root. A date subfolder nested below the category is not a
    category, and the ``use_date_subfolders`` flag changes nothing — this
    locks the simplification of ``_file_category``.
    """
    root = tmp_path / "out"
    _write(root, *sets)
    data = _payload(tmp_path, use_date_subfolders=use_date_subfolders)
    by_name = _by_name(_rows(data))
    assert by_name[list(sets)[-1]]["category"] == expected



