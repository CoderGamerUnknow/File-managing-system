"""Rule-based file matching: extension rules and ignore glob patterns."""
from __future__ import annotations

import fnmatch
from pathlib import Path


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
