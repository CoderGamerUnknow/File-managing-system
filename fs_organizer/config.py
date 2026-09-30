"""Configuration loading, defaults, and validation for fs-organizer."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
    api_key: Optional[str] = None
    model: str = "llama3.2"
    base_url: Optional[str] = None
    timeout_seconds: float = 15.0
    max_bytes_to_read: int = 65536
    allowed_subfolders: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AIConfig":
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
        )


@dataclass
class JournalConfig:
    """Persisted move journal (JSONL audit trail + dashboard date source)."""

    enabled: bool = False
    path: Optional[str] = None  # default: ~/.fs-organizer/moves.jsonl

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JournalConfig":
        return cls(
            enabled=_coerce_bool(data.get("enabled", False), "journal.enabled"),
            path=_coerce_non_empty_str_or_none(data.get("path"), "journal.path"),
        )

    def resolved_path(self) -> Path:
        from .journal import DEFAULT_JOURNAL_PATH

        return Path(os.path.expanduser(self.path)) if self.path else DEFAULT_JOURNAL_PATH()


@dataclass
class Config:
    watch_folders: list[str] = field(default_factory=list)
    target_rules: dict[str, str] = field(default_factory=dict)
    ignore_patterns: list[str] = field(default_factory=list)
    target_root: Optional[str] = None
    use_date_subfolders: bool = False
    dry_run: bool = False
    file_stable_seconds: float = 1.0
    recursive: bool = False  # opt-in: also watch/organize subfolders of watch folders
    ai: AIConfig = field(default_factory=AIConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)

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
        }

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


def _coerce_non_empty_str_or_none(value: Any, key: str) -> Optional[str]:
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
    )


def load_config(path: str | Path) -> Config:
    """Load and validate a JSON config file into a Config object."""
    return from_dict(_load_json(Path(path)))
