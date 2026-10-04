"""Rule-based file matching: extension rules and ignore glob patterns.

Also owns :func:`normalize_path_key`, the single canonical form for
"which file/directory is this?" comparisons (see its docstring).
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path


def strip_extended_prefix(p: Path | str) -> str:
    """Drop the Windows extended-length prefix, keeping the original spelling.

    ``\\\\?\\C:\\...`` -> ``C:\\...`` and ``\\\\?\\UNC\\server\\share`` ->
    ``\\\\server\\share``. Unlike :func:`normalize_path_key` this does NOT fold
    case, so it is the one to use when the original spelling matters (the
    journal's category label, for instance).

    Pure string work - no filesystem access, never raises.
    """
    s = str(p)
    while True:
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[8:]
        elif s.startswith("\\\\?\\"):
            s = s[4:]
        else:
            return s


def normalize_path_key(p: Path | str) -> str:
    """Canonical string identity for a path - the ONLY form used to key state.

    Windows ``Path.resolve()`` intermittently returns the extended-path form
    (``\\\\?\\C:\\...``) depending on what exists at resolve time, so the same
    file can spell two different ways within a single run. Any map, set, or
    cache keyed by a raw ``Path`` would then treat one file as two (the
    original data-loss bug: flaw #51). This strips the extended prefix (and its
    UNC variant) and folds case, so every spelling of one path produces one
    key.

    Pure string work - no filesystem access, never raises.
    """
    return os.path.normcase(strip_extended_prefix(p))


def is_under(key: str, root_key: str) -> bool:
    """True if canonical *key* is *root_key* itself or lives beneath it.

    Both arguments must be :func:`normalize_path_key` output. This is the one
    correct way to do a prefix test on those keys: naively appending
    ``os.sep`` to a root that already ends in a separator (``/`` or ``C:\\``)
    builds a prefix (``//`` / ``c:\\\\``) that NO child path can match, which
    would silently make the watcher ignore every event - or let the loop
    guard miss the organizer's own output - when watching a filesystem or
    drive root.
    """
    if key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return key.startswith(prefix)


def is_ignored(path: Path, ignore_patterns: list[str]) -> bool:
    """True if the path matches any ignore glob.

    Patterns containing a slash (``/`` or ``\\``) match against the full path
    (normalized to forward slashes); bare-name patterns match ONLY the file
    name — so a name pattern like ``c*`` can never accidentally ignore every
    file whose *path* happens to start with the drive letter.
    """
    name = path.name
    posix_full = path.as_posix()
    for pattern in ignore_patterns:
        normalized = pattern.replace("\\", "/")
        if "/" in normalized:
            if fnmatch.fnmatch(posix_full, normalized):
                return True
        elif fnmatch.fnmatch(name, normalized):
            return True
    return False


def from_list(rules: dict[str, str]) -> dict[str, str]:
    """Normalise a raw ``{extension: category}`` dict into the canonical form.

    - extension keys are lower-cased,
    - category values are stripped of surrounding whitespace,
    - a ``None``/non-string category is dropped (never stored).

    This is a pure helper used by the dashboard's rule-editing and by
    config merging; it never touches the filesystem.
    """
    clean: dict[str, str] = {}
    for ext, category in rules.items():
        if not isinstance(ext, str) or not ext.startswith("."):
            continue
        if not isinstance(category, str) or not category.strip():
            continue
        clean[ext.lower()] = category.strip()
    return clean


def match_extension(path: Path, target_rules: dict[str, str]) -> str | None:
    """Return the category for this file's extension, or None if unknown."""
    ext = path.suffix.lower()
    return target_rules.get(ext)


def categories_for(rules: dict[str, str]) -> dict[str, list[str]]:
    """Invert extension rules to ``{category: [extension, ...]}``.

    ``categories_for({'.txt': 'Documents', '.png': 'Images'})`` ->
    ``{'Documents': ['.txt'], 'Images': ['.png']}``. Order follows the first
    time each extension was seen.
    """
    by_category: dict[str, list[str]] = {}
    for ext, cat in rules.items():
        by_category.setdefault(cat, []).append(ext)
    return {cat: sorted(exts) for cat, exts in by_category.items()}
