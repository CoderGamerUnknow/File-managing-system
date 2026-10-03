"""AI fallback classification for unknown extensions.

Uses only the standard library (urllib) so no extra dependencies are needed.
Supports two providers:
  - "openai": chat completions API with an API key
  - "ollama": local chat API at http://localhost:11434 by default
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import ai_cache
from .config import AIConfig

logger = logging.getLogger("fs_organizer")

_PROMPT_TEMPLATE = """You classify files by category for a file organizer.
Read the file preview below and reply with ONLY the best category from this list:
{categories}

File name: {filename}
File extension: {extension}

--- file preview (truncated) ---
{preview}
--- end of preview ---"""


def _read_preview(path: Path, max_bytes: int) -> str:
    """Read up to max_bytes of the file, decoded lossily, for the prompt."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _build_prompt(path: Path, config: AIConfig) -> str:
    categories = ", ".join(config.allowed_subfolders) or "Documents, Other"
    return _PROMPT_TEMPLATE.format(
        categories=categories,
        filename=path.name,
        extension=path.suffix or "(none)",
        preview=_read_preview(path, config.max_bytes_to_read),
    )


def _extract_json_object(text: str) -> dict:
    """Extract the first JSON object embedded in the model's reply."""
    text = text.strip()
    # Tolerate ```json fences and prose around the payload.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _request_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _classify_openai(path: Path, config: AIConfig) -> str:
    base = config.base_url or "https://api.openai.com/v1"
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OpenAI provider enabled but no api_key configured")
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": "You reply with a single JSON object and nothing else."},
            {"role": "user", "content": _build_prompt(path, config)},
        ],
        "temperature": 0,
    }
    data = _request_json(
        f"{base.rstrip('/')}/chat/completions",
        payload,
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        config.timeout_seconds,
    )
    content = data["choices"][0]["message"]["content"]
    return _extract_json_object(content).get("category", "")


def _classify_ollama(path: Path, config: AIConfig) -> str:
    base = config.base_url or "http://localhost:11434"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": "You reply with a single JSON object and nothing else."},
            {"role": "user", "content": _build_prompt(path, config)},
        ],
        "stream": False,
        "format": "json",
    }
    data = _request_json(
        f"{base.rstrip('/')}/api/chat", payload, {"Content-Type": "application/json"},
        config.timeout_seconds,
    )
    content = data["message"]["content"]
    return _extract_json_object(content).get("category", "")


def classify_with_ai(path: Path, config: AIConfig) -> str | None:
    """
    Classify a file via the configured AI provider.

    Returns an allowed category name, or None on any failure
    (network errors, bad replies, disallowed category, provider disabled).
    Failures are logged and never raise to the caller.
    """
    if not config.enabled:
        return None

    # V3: consult the persistent classification cache before paying for a
    # model request. A cached hit is the same answer the model would give
    # (the key hashes the exact preview window it sees).
    cache: dict[str, str] = {}
    key: str | None = None
    if getattr(config, "cache_enabled", False):
        cache = ai_cache.load(config.resolved_cache_path())
        key = ai_cache.cache_key(path, config)
        cached = ai_cache.get(cache, key)
        if cached is not None:
            logger.debug("AI cache hit for %s -> %s", path.name, cached)
            return cached

    provider = config.provider
    try:
        if provider == "openai":
            raw = _classify_openai(path, config)
        elif provider == "ollama":
            raw = _classify_ollama(path, config)
        else:
            logger.warning("Unknown AI provider: %s", provider)
            return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            OSError, RuntimeError) as exc:
        logger.warning("AI request failed for %s: %s", path.name, exc)
        return None
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("AI reply unparseable for %s: %s", path.name, exc)
        return None
    # AttributeError: null/malformed reply structure or a non-string base_url
    # on a hand-built AIConfig — still must never propagate.
    except AttributeError as exc:
        logger.warning("AI reply/config unusable for %s: %s", path.name, exc)
        return None

    # The reply's "category" value may be null or a non-string; coerce before
    # touching string methods (classify_with_ai must never raise).
    raw = str(raw).strip().strip('"\'')
    # Models are unreliable about casing; match the allow-list case-insensitively
    # and return its canonical spelling.
    allowed = {c.strip().lower(): c.strip() for c in config.allowed_subfolders if c.strip()}
    canonical = allowed.get(raw.lower())
    if canonical:
        # Only a successful, accepted answer is cached — a failure above was
        # returned early, so a transient outage never poisons the cache.
        if getattr(config, "cache_enabled", False) and ai_cache.put(cache, key, canonical):
            ai_cache.save(config.resolved_cache_path(), cache)
        return canonical
    logger.info("AI suggested %r for %s which is not an allowed category; ignoring",
                raw, path.name)
    return None
