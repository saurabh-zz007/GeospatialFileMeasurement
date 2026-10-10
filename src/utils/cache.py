import hashlib
import json
import threading
from typing import Any, Optional

from cachetools import TTLCache

from src.utils.settings import settings

_cache_lock = threading.Lock()
_cache: Optional[TTLCache] = None


def get_ttl_cache() -> TTLCache:
    """Returns or reinitializes the singleton TTLCache matching current settings."""
    global _cache
    if (
        _cache is None
        or _cache.maxsize != settings.CACHE_MAX_ENTRIES
        or _cache.ttl != settings.CACHE_TTL_SECONDS
    ):
        with _cache_lock:
            if (
                _cache is None
                or _cache.maxsize != settings.CACHE_MAX_ENTRIES
                or _cache.ttl != settings.CACHE_TTL_SECONDS
            ):
                _cache = TTLCache(
                    maxsize=max(1, settings.CACHE_MAX_ENTRIES),
                    ttl=max(1, settings.CACHE_TTL_SECONDS),
                )
    return _cache


def clear_cache() -> None:
    """Clears all cached items from the in-memory cache."""
    with _cache_lock:
        if _cache is not None:
            _cache.clear()


def generate_etag(data: Any) -> str:
    """Computes a deterministic strong ETag for serialized JSON data."""
    serialized = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f'"{hashlib.md5(serialized).hexdigest()}"'


def is_etag_match(client_etag: Optional[str], current_etag: str) -> bool:
    """
    Checks if client If-None-Match matches the current ETag,
    accounting for weak ETag prefixes ('W/') and quotes.
    """
    if not client_etag:
        return False

    clean_current = current_etag.strip()
    if clean_current.startswith("W/"):
        clean_current = clean_current[2:]
    clean_current = clean_current.strip('"')

    for part in client_etag.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("W/"):
            part = part[2:]
        if part.strip('"') == clean_current:
            return True
    return False


def get_cached_entry(key: str) -> Optional[dict[str, Any]]:
    """Retrieves cached payload if caching is enabled."""
    if not settings.CACHE_ENABLED:
        return None
    cache = get_ttl_cache()
    with _cache_lock:
        return cache.get(key)


def set_cached_entry(key: str, data: dict[str, Any], etag: str) -> None:
    """Stores payload and etag into TTLCache if caching is enabled."""
    if not settings.CACHE_ENABLED:
        return
    cache = get_ttl_cache()
    with _cache_lock:
        cache[key] = {"data": data, "etag": etag}
