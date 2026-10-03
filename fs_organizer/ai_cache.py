"""Persistent cache for AI classifications (V3).

The AI fallback is the most expensive thing fs-organizer does: every unknown
file triggers a model request. Many unknown files repeat (a stream of ``.dat``
exports, a folder of the same report format), so caching the answer for the
*content* is both a large latency win and a direct expression of the
low-resource-daemon skill.

Design:

- The key is a SHA-256 over the model identity plus the file's extension plus
  a streamed hash of its first ``max_bytes_to_read`` bytes. Hashing only the
  preview window is deliberate: it is exactly the input the model sees, so two
  files that hash the same would produce the same prompt.
- The cache is a single JSON object (``{key: category}``), written atomically
  (temp file + ``os.replace``) under a lock so concurrent workers cannot
  interleave a partial write.
- Every operation is best-effort: an unwritable or corrupt cache degrades to
  "no cache" with a warning and must never break a move or a classification.

Only *successful* classifications are cached; a failed request is retried
next time (a transient outage must not poison the cache with a permanent
"unknown").
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("fs_organizer")

_cache_lock = threading.Lock()

_HASH_CHUNK = 64 * 1024


def _content_digest(path: Path, max_bytes: int) -> str | None:
    """SHA-256 over the first ``max_bytes`` of *path*, or None if unreadable."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            remaining = max(1, max_bytes)
            while remaining > 0:
                chunk = fh.read(min(_HASH_CHUNK, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def cache_key(path: Path, config) -> str | None:
    """Stable key for *path* under *config*'s AI policy, or None if unreadable.

    Includes provider + model so switching models never serves a stale answer
    produced by a different one.
    """
    digest = _content_digest(path, getattr(config, "max_bytes_to_read", 65536))
    if digest is None:
        return None
    identity = f"{config.provider}\x00{config.model}\x00{path.suffix.lower()}\x00{digest}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def load(path: Path) -> dict[str, str]:
    """Read the cache file; a missing/corrupt file yields an empty cache."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read AI cache %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def save(path: Path, cache: dict[str, str]) -> None:
    """Atomically persist *cache*; never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        with _cache_lock:
            with temp.open("w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
            os.replace(str(temp), str(path))
    except OSError as exc:
        logger.warning("Could not write AI cache %s: %s", path, exc)


def get(cache: dict[str, str], key: str | None) -> str | None:
    if key is None:
        return None
    return cache.get(key)


def put(cache: dict[str, str], key: str | None, category: str) -> bool:
    """Record *category* for *key*; returns True when the cache changed."""
    if key is None or not category:
        return False
    if cache.get(key) == category:
        return False
    cache[key] = category
    return True
