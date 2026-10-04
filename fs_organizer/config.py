"""Configuration loading, defaults, and validation for fs-organizer."""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .rules import categories_for

DEFAULT_IGNORE_PATTERNS = [
    ".*",
    "*.tmp",
    "*.part",
    "*.crdownload",
    "*~",
    "desktop.ini",
    "Thumbs.db",
]

DEFAULT_TARGET_RULES: dict[str, str] = {
    ".jpg": "Images",
    ".jpeg": "Images",
    ".png": "Images",
    ".gif": "Images",
    ".webp": "Images",
    ".svg": "Images",
    ".pdf": "Documents",
    ".docx": "Documents",
    ".txt": "Documents",
    ".md": "Documents",
    ".xlsx": "Documents",
    ".pptx": "Documents",
    ".mp3": "Music",
    ".wav": "Music",
    ".flac": "Music",
    ".mp4": "Videos",
    ".mkv": "Videos",
    ".mov": "Videos",
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".7z": "Archives",
    ".exe": "Installers",
    ".msi": "Installers",
    ".iso": "Disk Images",
}


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


class ConfigFileNotFoundError(ConfigError):
    """The configured JSON file is missing."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"Config file not found: {path}")
        self.path = path


class ConfigInvalidError(ConfigError):
    """The configured JSON file is malformed or semantically invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class AIConfig:
    enabled: bool = False
    provider: str = "ollama"  # "openai" | "ollama"
    api_key: str | None = None
    model: str = "llama3.2"
    base_url: str | None = None
    timeout_seconds: float = 15.0
    max_bytes_to_read: int = 65536
    allowed_subfolders: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    # V3: cache successful classifications so repeat files never re-hit the
    # model (low-resource-daemon skill). Off by default — the cache writes a
    # file next to the journal, so enabling it is an explicit opt-in.
    cache_enabled: bool = False
    cache_path: str | None = None

    def resolved_cache_path(self) -> Path:
        """Where the classification cache lives (created on first write)."""
        if self.cache_path:
            return Path(os.path.expanduser(self.cache_path))
        return Path.home() / ".fs-organizer" / "ai_cache.json"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AIConfig:
        """Build an AIConfig from a raw ``ai`` JSON object (validates each field)."""
        return cls(
            enabled=_coerce_bool(data.get("enabled", False), "ai.enabled"),
            provider=_coerce_non_empty_str(data.get("provider", "ollama"), "ai.provider"),
            api_key=_coerce_non_empty_str_or_none(data.get("api_key"), "ai.api_key"),
            model=_coerce_non_empty_str(data.get("model", "llama3.2"), "ai.model"),
            base_url=_coerce_non_empty_str_or_none(data.get("base_url"), "ai.base_url"),
            timeout_seconds=_coerce_non_negative_number(
                data.get("timeout_seconds", 15.0), "ai.timeout_seconds"
            ),
            max_bytes_to_read=int(
                _coerce_non_negative_number(
                    data.get("max_bytes_to_read", 65536), "ai.max_bytes_to_read"
                )
            ),
            allowed_subfolders=data.get("allowed_subfolders", []),
            extensions=[
                e.lower() for e in data.get("extensions", []) or [] if isinstance(e, str)
            ],
            cache_enabled=_coerce_bool(
                data.get("cache_enabled", False), "ai.cache_enabled"
            ),
            cache_path=_coerce_non_empty_str_or_none(
                data.get("cache_path"), "ai.cache_path"
            ),
        )


@dataclass
class JournalConfig:
    """Persisted move journal (JSONL audit trail + dashboard date source)."""

    enabled: bool = False
    path: str | None = None  # default: ~/.fs-organizer/moves.jsonl

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalConfig:
        return cls(
            enabled=_coerce_bool(data.get("enabled", False), "journal.enabled"),
            path=_coerce_non_empty_str_or_none(data.get("path"), "journal.path"),
        )

    def resolved_path(self) -> Path:
        from .journal import DEFAULT_JOURNAL_PATH

        return Path(os.path.expanduser(self.path)) if self.path else DEFAULT_JOURNAL_PATH()


@dataclass
class AgePolicy:
    """V2 file-age policy: don't organize files that are too new or too old.

    ``min_age_seconds`` protects files still being written (a second line of
    defense behind the debouncer); ``max_age_days`` keeps ancient files
    where the user left them (a mailbox of 10-year-old downloads stays
    put). 0 / None disable each bound. Both bounds are evaluated against
    the file's mtime at decision time.
    """

    min_age_seconds: float = 0.0            # 0 = no lower bound
    max_age_days: float | None = None    # None = no upper bound

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgePolicy:
        if not isinstance(data, dict):
            raise ConfigInvalidError(
                f"'age_policy' must be an object, got {type(data).__name__}"
            )
        return cls(
            min_age_seconds=_coerce_non_negative_number(
                data.get("min_age_seconds", 0.0), "age_policy.min_age_seconds"
            ),
            max_age_days=(
                None
                if data.get("max_age_days") is None
                else _coerce_non_negative_number(
                    data.get("max_age_days"), "age_policy.max_age_days"
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_age_seconds": self.min_age_seconds,
            "max_age_days": self.max_age_days,
        }

    def violates(self, mtime: float, now: float) -> bool:
        """True when the file's age is outside the allowed window."""
        age = now - mtime
        return age < self.min_age_seconds or (
            self.max_age_days is not None and age > self.max_age_days * 86400.0
        )


@dataclass
class DestinationTemplate:
    """V2 destination template: where organized files land.

    Tokens: ``{category}`` and ``{date:FORMAT}`` (FORMAT is a strftime
    pattern applied to the file's mtime; default ``%Y-%m``). The template
    is evaluated relative to the target root; ``{category}`` must appear
    (otherwise every category would share one folder) and the resolved
    path must stay inside the target root — the mover's traversal guard
    enforces that on every move, and load_config refuses templates whose
    static part escapes the root up front.
    """

    pattern: str = "{category}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DestinationTemplate:
        if not isinstance(data, dict):
            raise ConfigInvalidError(
                f"'destination_template' must be an object, got {type(data).__name__}"
            )
        pattern = data.get("pattern", "{category}")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigInvalidError(
                "'destination_template.pattern' must be a non-empty string"
            )
        pattern = pattern.strip().replace("\\", "/")
        if pattern.startswith("/"):
            raise ConfigInvalidError(
                "'destination_template.pattern' must be relative to the target root"
            )
        if "{category}" not in pattern:
            raise ConfigInvalidError(
                "'destination_template.pattern' must contain {category} — "
                "otherwise every category would share one folder"
            )
        # Refuse strftime formats that produce path separators inside a
        # single {date:...} token (e.g. %Y/%m is fine as separate text, but
        # a slash inside the token would make the date a folder — that is
        # the user's choice via the pattern text itself, not the format).
        return cls(pattern=pattern)

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern}

    def render(self, category: str, mtime: float) -> str:
        """Render the template to a relative path (forward-slash separated).

        Unknown tokens are left as literal text; malformed {date:...}
        formats fall back to %Y-%m so a bad format can never crash a move.
        """
        out: list[str] = []
        for part in self.pattern.split("/"):
            rendered = part
            if "{date" in part:
                start = part.find("{date")
                end = part.find("}", start)
                if end == -1:
                    # Unterminated token (e.g. "{date:%Y/%m}" — the slash
                    # split it before its closing brace). The previous code
                    # used part[end + 1:] with end == -1, which is part[0:],
                    # so the malformed token was appended a SECOND time:
                    # "{date:%Y/%m}" rendered as
                    # "2025-12{date:%Y/%m}" — a literal garbage folder name
                    # (flaw #48). Drop the malformed remainder instead and
                    # fall back to the safe default format.
                    rendered = part[:start] + datetime.fromtimestamp(mtime).strftime(
                        "%Y-%m"
                    )
                else:
                    token = part[start : end + 1]
                    spec = token[5:-1]  # strip {date and }
                    fmt = spec[1:] if spec.startswith(":") and spec[1:] else "%Y-%m"
                    try:
                        rendered = (
                            part[:start]
                            + datetime.fromtimestamp(mtime).strftime(fmt)
                            + part[end + 1 :]
                        )
                    except (ValueError, TypeError):
                        rendered = (
                            part[:start]
                            + datetime.fromtimestamp(mtime).strftime("%Y-%m")
                            + part[end + 1 :]
                        )
            out.append(rendered.replace("{category}", category))
        return "/".join(out)


@dataclass
class SubRule:
    """V2 per-category sub-rule: route files differently by source folder.

    ``{"pattern": "**/invoices/**", "extensions": [".pdf"], "category": "Invoices"}``
    sends PDFs landing under a ``Downloads/invoices`` folder to the
    ``Invoices`` category, while every other PDF still follows the global
    rule table.

    Matching is extension-first: the file's extension must be in
    ``extensions`` (case-insensitive), then the *source* path is matched
    against ``pattern`` with the same convention as ``ignore_patterns`` —
    a pattern containing a slash matches the full path, a bare pattern
    matches the file name. Evaluation order is the list order; the first
    match wins (visible in ``check`` output).

    Patterns are matched with ``fnmatch`` against the already-resolved path
    and a leading ``~/`` IS expanded to the home directory, exactly as
    ``watch_folders`` and ``target_root`` are. Note also that ``fnmatch``
    has no notion of ``**``: it is two ordinary ``*`` wildcards.
    """

    pattern: str = ""
    extensions: list[str] = field(default_factory=list)
    category: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> SubRule:
        where = f"sub_rules[{index}]"
        if not isinstance(data, dict):
            raise ConfigInvalidError(f"'{where}' must be an object, got {type(data).__name__}")
        pattern = data.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigInvalidError(f"'{where}.pattern' must be a non-empty glob string")
        category = data.get("category")
        if not isinstance(category, str) or not category.strip():
            raise ConfigInvalidError(f"'{where}.category' must be a non-empty category name")
        exts = data.get("extensions", [])
        if (
            not isinstance(exts, list)
            or not exts
            or not all(isinstance(e, str) and e.startswith(".") for e in exts)
        ):
            raise ConfigInvalidError(
                f"'{where}.extensions' must be a non-empty list of extensions like '.pdf'"
            )
        return cls(
            pattern=pattern.strip(),
            extensions=[e.lower() for e in exts],
            category=category.strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "extensions": list(self.extensions),
            "category": self.category,
        }

    def match_pattern(self) -> str:
        """``pattern`` prepared for ``fnmatch`` against a resolved posix path.

        Two transformations, both needed for a glob written by a human to
        work the same way it does for ``watch_folders``:

        - **Backslashes become forward slashes.** ``category_for`` matches
          against ``Path.as_posix()``, and a Windows-style pattern such as
          ``~\\Downloads\\**`` would otherwise never line up.
        - **A genuine home reference is expanded** (``~``, ``~/...``,
          ``~\\...``) so ``~/Downloads/invoices/**`` behaves like every other
          path field in the config.

        **Order matters: separators are folded BEFORE the expansion.**
        ``posixpath.expanduser`` (Linux, macOS) only recognizes ``~`` at the
        start of a string or ``~/...`` — a backslash is a legal *filename*
        character there, so it is not a separator and ``~\\Downloads\\**``
        would come back untouched, leaving the rule silently unable to match
        (the file then falls back to the global category). Folding first
        turns it into ``~/...``, which every platform's ``expanduser``
        understands. The result is folded again because Windows
        ``expanduser`` hands back native ``\\`` separators.

        The guard on the expansion is deliberate and NOT paranoia:
        ``os.path.expanduser("~scan.pdf")`` returns ``C:\\Users\\scan.pdf``
        on Windows, because CPython reads everything after a lone ``~`` as
        a *username*. A bare ``~name`` pattern carries no slash, so it is a
        file-NAME glob where ``~`` is a perfectly legal character — expanding
        it would silently rewrite the user's rule. So only a lone ``~`` or a
        ``~`` immediately followed by a separator (after folding) counts as
        home.

        ``pattern`` itself is left exactly as authored, so ``to_dict()``
        round-trips the user's own text and ``check --json`` never leaks an
        absolute home path.
        """
        pattern = self.pattern.replace("\\", "/")
        if pattern == "~" or pattern.startswith("~/"):
            pattern = os.path.expanduser(pattern)
        return pattern.replace("\\", "/")


@dataclass
class NameRule:
    """V3 name/content rule: route a file by its *name*, not just extension.

    ``{"pattern": "invoice*", "category": "Invoices"}`` sends every file
    whose name matches the glob to ``Invoices`` regardless of extension, and
    ``{"pattern": "^IMG_\\d+", "category": "Photos", "regex": true}``
    matches the name as a regular expression. ``extensions`` optionally
    narrows the rule to a set of suffixes (empty = any extension).

    Name rules are evaluated after ``sub_rules`` and before the global
    ``target_rules`` table, in list order, first match wins — the same
    precedence model as everything else in the config.
    """

    pattern: str = ""
    category: str = ""
    regex: bool = False
    extensions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> NameRule:
        where = f"name_rules[{index}]"
        if not isinstance(data, dict):
            raise ConfigInvalidError(f"'{where}' must be an object, got {type(data).__name__}")
        pattern = data.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigInvalidError(f"'{where}.pattern' must be a non-empty string")
        category = data.get("category")
        if not isinstance(category, str) or not category.strip():
            raise ConfigInvalidError(f"'{where}.category' must be a non-empty category name")
        regex = _coerce_bool(data.get("regex", False), f"{where}.regex")
        exts = data.get("extensions", [])
        if not isinstance(exts, list) or not all(
            isinstance(e, str) and e.startswith(".") for e in exts
        ):
            raise ConfigInvalidError(
                f"'{where}.extensions' must be a list of extensions like '.pdf'"
            )
        pattern = pattern.strip()
        if regex:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigInvalidError(
                    f"'{where}.pattern' is not a valid regular expression: {exc}"
                ) from exc
        return cls(
            pattern=pattern,
            category=category.strip(),
            regex=regex,
            extensions=[e.lower() for e in exts],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "category": self.category,
            "regex": self.regex,
            "extensions": list(self.extensions),
        }

    def matches(self, path: Path) -> bool:
        """True when *path*'s name matches this rule (and its extension is allowed)."""
        # Case-tolerant on both sides: from_dict() normalizes the list, but a
        # hand-built rule (embedded config, tests) may hold ".PDF" as authored.
        if self.extensions and path.suffix.lower() not in {
            e.lower() for e in self.extensions
        }:
            return False
        if self.regex:
            return re.search(self.pattern, path.name) is not None
        return fnmatch.fnmatch(path.name, self.pattern)


@dataclass
class QuietHours:
    """V3 quiet hours: organize only within a daily time window.

    ``{"start": "08:00", "end": "22:00"}`` organizes only between 08:00 and
    22:00 local time; outside it, incoming events are left in place (the
    watcher simply does not act — files are not held, so a file that arrives
    at 23:00 is picked up by the next scan after the window opens). ``start``
    and ``end`` are ``HH:MM``. A window whose end is <= its start wraps
    midnight (``22:00``-``06:00``). Disabled when both are unset.
    """

    start: str | None = None
    end: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuietHours:
        if not isinstance(data, dict):
            raise ConfigInvalidError(
                f"'quiet_hours' must be an object, got {type(data).__name__}"
            )
        start = data.get("start")
        end = data.get("end")
        if start is None and end is None:
            return cls()
        if not isinstance(start, str) or not isinstance(end, str):
            raise ConfigInvalidError(
                "'quiet_hours' requires both 'start' and 'end' as 'HH:MM' strings"
            )
        for label, value in (("start", start), ("end", end)):
            if _parse_hhmm(value) is None:
                raise ConfigInvalidError(
                    f"'quiet_hours.{label}' must be 'HH:MM' (00:00-23:59), got {value!r}"
                )
        return cls(start=start.strip(), end=end.strip())

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end}

    @property
    def enabled(self) -> bool:
        return self.start is not None and self.end is not None

    def allows(self, now: datetime | None = None) -> bool:
        """True when *now* (default: local time) is inside the window."""
        if not self.enabled:
            return True
        now = now or datetime.now()
        current = now.hour * 60 + now.minute
        start = _parse_hhmm(self.start)
        end = _parse_hhmm(self.end)
        if start is None or end is None:
            return True
        if start <= end:
            return start <= current <= end
        # Window wraps midnight (e.g. 22:00-06:00).
        return current >= start or current <= end


def _parse_hhmm(value: str) -> int | None:
    """'HH:MM' -> minutes since midnight, or None when malformed."""
    try:
        hours, minutes = value.split(":")
        h, m = int(hours), int(minutes)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h * 60 + m
    return None


@dataclass
class Config:
    watch_folders: list[str] = field(default_factory=list)
    target_rules: dict[str, str] = field(default_factory=dict)
    ignore_patterns: list[str] = field(default_factory=list)
    target_root: str | None = None
    use_date_subfolders: bool = False
    dry_run: bool = False
    file_stable_seconds: float = 1.0
    recursive: bool = False  # opt-in: also watch/organize subfolders of watch folders
    ai: AIConfig = field(default_factory=AIConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    age_policy: AgePolicy = field(default_factory=AgePolicy)
    destination_template: DestinationTemplate = field(default_factory=DestinationTemplate)
    sub_rules: list[SubRule] = field(default_factory=list)
    name_rules: list[NameRule] = field(default_factory=list)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    disk_space_guard: float = 0.0  # V3: refuse moves when dest volume free% < this

    def resolved_watch_folders(self) -> list[Path]:
        """Existing watch folders, resolved (the set the organizer acts on)."""
        out: list[Path] = []
        for f in self.watch_folders:
            p = Path(os.path.expanduser(f))
            if p.is_dir():
                out.append(p.resolve())
        return out

    def expanded_watch_folders(self) -> list[Path]:
        """Raw expanded watch folders (before existence resolution)."""
        return [Path(os.path.expanduser(f)) for f in self.watch_folders]

    def resolved_target_root(self) -> Path:
        base = self.target_root if self.target_root else "~/Organized"
        return Path(os.path.expanduser(base)).resolve()

    def effective_ignore_patterns(self) -> list[str]:
        """The one ignore list: built-in defaults + user patterns.

        This is the list the organizer, the mover, and every scan apply; the
        diagnostics display exactly the same thing, so "ignored" always means
        the same thing everywhere.
        """
        return list(DEFAULT_IGNORE_PATTERNS) + list(self.ignore_patterns)

    def to_dict(self) -> dict[str, object]:
        """Export the resolved configuration as a JSON-safe dict.

        ``ai.api_key`` is redacted: this dict feeds ``check --json`` and the
        diagnostics output, which must never print secrets.
        """
        ai = self.ai
        return {
            "watch_folders": [os.path.expanduser(f) for f in self.watch_folders],
            "target_rules": dict(self.target_rules),
            "ignore_patterns": list(self.ignore_patterns),
            "target_root": self.target_root,
            "use_date_subfolders": self.use_date_subfolders,
            "dry_run": self.dry_run,
            "file_stable_seconds": self.file_stable_seconds,
            "ai": {
                "enabled": ai.enabled,
                "provider": ai.provider,
                "api_key": "***" if ai.api_key else None,
                "model": ai.model,
                "base_url": ai.base_url,
                "timeout_seconds": ai.timeout_seconds,
                "max_bytes_to_read": ai.max_bytes_to_read,
                "allowed_subfolders": list(ai.allowed_subfolders),
                "extensions": list(ai.extensions),
            },
            "recursive": self.recursive,
            "journal": {
                "enabled": self.journal.enabled,
                "path": self.journal.path,
            },
            "age_policy": self.age_policy.to_dict(),
            "destination_template": self.destination_template.to_dict(),
            "sub_rules": [r.to_dict() for r in self.sub_rules],
            "name_rules": [r.to_dict() for r in self.name_rules],
            "quiet_hours": self.quiet_hours.to_dict(),
            "disk_space_guard": self.disk_space_guard,
        }

    def category_for(self, path: Path) -> str | None:
        """The category for *path*: first matching sub-rule, else the table.

        Sub-rules are evaluated in config order; a rule matches when the
        file's extension is in the rule's list AND its path matches the
        rule's glob (same slash convention as ignore patterns). The global
        ``target_rules`` table is the fallback. Pure path/string work —
        never touches the filesystem.
        """
        ext = path.suffix.lower()
        if self.sub_rules:
            posix_full = path.as_posix()
            for rule in self.sub_rules:
                if ext not in rule.extensions:
                    continue
                normalized = rule.match_pattern()
                if "/" in normalized:
                    if fnmatch.fnmatch(posix_full, normalized):
                        return rule.category
                elif fnmatch.fnmatch(path.name, normalized):
                    return rule.category
        # V3 name/content rules: match by file name (glob or regex), before
        # the extension table but after sub-rules (source-folder routing wins).
        for rule in self.name_rules:
            if rule.matches(path):
                return rule.category
        return self.target_rules.get(ext)

    def coverage_report(self) -> dict[str, object]:
        """Deterministic coverage report over the resolved config fields.

        ``categories`` maps category -> extensions the *rules* cover (AI-able
        extensions are not a category — the model decides at runtime).
        ``mismatched_categories`` is the symmetric difference between the
        categories the rules define and the categories the AI may pick, i.e.
        the names that can never be matched on one side of the policy.
        """
        rule_categories = set(self.target_rules.values())
        ai_categories = {c.strip() for c in self.ai.allowed_subfolders if c.strip()}
        return {
            "categories": categories_for(self.target_rules),
            "known_extensions": dict(self.target_rules),
            "uncovered": sorted(e.lower() for e in self.ai.extensions or []),
            "ai_covered": {
                e.lower(): self.target_rules[e.lower()]
                for e in self.ai.extensions or []
                if e.lower() in self.target_rules
            },
            "ai_enabled": self.ai.enabled,
            "provider": self.ai.provider,
            "model": self.ai.model,
            "rule_categories": sorted(rule_categories),
            "ai_categories": sorted(ai_categories),
            "mismatched_categories": sorted(rule_categories ^ ai_categories),
        }


def _coerce_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigInvalidError(f"'{key}' must be a boolean, got {value!r}")


def _coerce_non_negative_number(value: Any, key: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    raise ConfigInvalidError(f"'{key}' must be a non-negative number, got {value!r}")


def _coerce_non_empty_str(value: Any, key: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigInvalidError(f"'{key}' must be a non-empty string, got {value!r}")


def _coerce_non_empty_str_or_none(value: Any, key: str) -> str | None:
    """Non-empty string, or None for an unset/optional value (e.g. base_url)."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigInvalidError(f"'{key}' must be a non-empty string or null, got {value!r}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigFileNotFoundError(path) from exc
    except json.JSONDecodeError as exc:
        raise ConfigInvalidError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigInvalidError(
            f"Config root must be a JSON object, got {type(data).__name__}"
        )
    return data


def _validate_ai(ai: AIConfig) -> None:
    """Cross-field AI policy validation (runs after per-field coercion)."""
    if ai.provider not in ("openai", "ollama"):
        raise ConfigInvalidError("'ai.provider' must be 'openai' or 'ollama'")
    if ai.enabled and ai.provider == "openai" and not ai.api_key:
        raise ConfigInvalidError("'ai.api_key' is required when provider is 'openai'")
    if ai.enabled and not ai.allowed_subfolders:
        raise ConfigInvalidError(
            "'ai.allowed_subfolders' must be a non-empty list of categories "
            "when 'ai.enabled' is true — otherwise no AI answer can ever be accepted"
        )


def _clean_rules(rules: Any) -> dict[str, str]:
    if not isinstance(rules, dict):
        raise ConfigInvalidError(
            "'target_rules' must be an object mapping extensions to categories"
        )
    clean: dict[str, str] = {}
    for ext, category in rules.items():
        if not isinstance(ext, str) or not ext.startswith("."):
            raise ConfigInvalidError(
                f"Rule key must be an extension starting with '.': {ext!r}"
            )
        if not isinstance(category, str) or not category.strip():
            raise ConfigInvalidError(
                f"Rule value for {ext!r} must be a non-empty category name"
            )
        clean[ext.lower()] = category.strip()
    return clean


def _clean_ignore(ignore: Any) -> list[str]:
    if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
        raise ConfigInvalidError("'ignore_patterns' must be a list of glob strings")
    return list(ignore)


def _clean_ai_dict(ai_data: Any) -> dict[str, Any]:
    """Validate the shape of the raw ``ai`` object (None is normalized by the caller)."""
    if not isinstance(ai_data, dict):
        raise ConfigInvalidError(
            f"'ai' must be an object mapping AI settings, got {type(ai_data).__name__}"
        )
    allowed = ai_data.get("allowed_subfolders", [])
    if not isinstance(allowed, list) or not all(isinstance(s, str) and s for s in allowed):
        raise ConfigInvalidError(
            "'ai.allowed_subfolders' must be a list of non-empty strings"
        )
    exts = ai_data.get("extensions", [])
    if not isinstance(exts, list) or not all(isinstance(e, str) and e.startswith(".") for e in exts):
        raise ConfigInvalidError("'ai.extensions' must be a list of extensions like '.txt'")
    return ai_data


def from_dict(data: dict[str, Any]) -> Config:
    """Build a validated Config from a raw JSON object.

    Callers that already hold the decoded JSON (e.g. an embedded config in a
    web dashboard, or a test) can avoid writing a temp file. This is the
    reverse of to_dict (minus the redacted api_key), so a round trip through
    load_config always yields an AIConfig.
    """
    watch_folders = data.get("watch_folders")
    if not isinstance(watch_folders, list) or not watch_folders:
        raise ConfigInvalidError("'watch_folders' must be a non-empty list of folder paths")
    for f in watch_folders:
        if not isinstance(f, str) or not f.strip():
            raise ConfigInvalidError(f"Invalid watch folder entry: {f!r}")
        p = Path(os.path.expanduser(f))
        if not p.is_dir():
            raise ConfigInvalidError(f"Watch folder does not exist: {f}")

    rules = data.get("target_rules", dict(DEFAULT_TARGET_RULES))
    target_rules = _clean_rules(rules)

    ignore = data.get("ignore_patterns", list(DEFAULT_IGNORE_PATTERNS))
    ignore_patterns = _clean_ignore(ignore)

    target_root = data.get("target_root")
    if target_root is not None and (
        not isinstance(target_root, str) or not target_root.strip()
    ):
        raise ConfigInvalidError("'target_root' must be a non-empty string or null")

    ai_data = data.get("ai")
    if ai_data is None:
        ai_data = {}  # "ai": null means unset
    ai = AIConfig.from_dict(_clean_ai_dict(ai_data))
    _validate_ai(ai)

    journal_data = data.get("journal")
    if journal_data is None:
        journal = JournalConfig()
    else:
        if not isinstance(journal_data, dict):
            raise ConfigInvalidError(
                f"'journal' must be an object, got {type(journal_data).__name__}"
            )
        journal = JournalConfig.from_dict(journal_data)

    age_data = data.get("age_policy")
    age_policy = AgePolicy.from_dict(age_data) if age_data is not None else AgePolicy()

    template_data = data.get("destination_template")
    template = (
        DestinationTemplate.from_dict(template_data)
        if template_data is not None
        else DestinationTemplate()
    )

    sub_rules_data = data.get("sub_rules", [])
    if not isinstance(sub_rules_data, list):
        raise ConfigInvalidError(
            f"'sub_rules' must be a list of rule objects, got {type(sub_rules_data).__name__}"
        )
    sub_rules = [
        SubRule.from_dict(r, i) for i, r in enumerate(sub_rules_data) if r is not None
    ]

    name_rules_data = data.get("name_rules", [])
    if not isinstance(name_rules_data, list):
        raise ConfigInvalidError(
            f"'name_rules' must be a list of rule objects, got {type(name_rules_data).__name__}"
        )
    name_rules = [
        NameRule.from_dict(r, i) for i, r in enumerate(name_rules_data) if r is not None
    ]

    quiet_data = data.get("quiet_hours")
    quiet_hours = QuietHours.from_dict(quiet_data) if quiet_data is not None else QuietHours()

    return Config(
        watch_folders=watch_folders,
        target_rules=target_rules,
        ignore_patterns=ignore_patterns,
        target_root=target_root,
        use_date_subfolders=_coerce_bool(
            data.get("use_date_subfolders", False), "use_date_subfolders"
        ),
        dry_run=_coerce_bool(data.get("dry_run", False), "dry_run"),
        file_stable_seconds=_coerce_non_negative_number(
            data.get("file_stable_seconds", 1.0), "file_stable_seconds"
        ),
        recursive=_coerce_bool(data.get("recursive", False), "recursive"),
        ai=ai,
        journal=journal,
        age_policy=age_policy,
        destination_template=template,
        sub_rules=sub_rules,
        name_rules=name_rules,
        quiet_hours=quiet_hours,
        disk_space_guard=_coerce_non_negative_number(
            data.get("disk_space_guard", 0.0), "disk_space_guard"
        ),
    )


def _apply_env_overrides(config: Config) -> Config:
    """Apply ``FSORG_*`` environment overrides for scripting/CI.

    Only two knobs are exposed, both of which are safe to force from the
    environment: ``FSORG_DRY_RUN`` (force a preview) and ``FSORG_TARGET_ROOT``
    (redirect output). They override the file values for this process only;
    nothing is written back. Truthy values are ``1/true/yes/on``.
    """
    dry = os.environ.get("FSORG_DRY_RUN")
    if dry is not None:
        config.dry_run = dry.strip().lower() in ("1", "true", "yes", "on")
    root = os.environ.get("FSORG_TARGET_ROOT")
    if root and root.strip():
        config.target_root = root.strip()
    return config


def load_config(path: str | Path) -> Config:
    """Load and validate a JSON config file into a Config object."""
    return _apply_env_overrides(from_dict(_load_json(Path(path))))
