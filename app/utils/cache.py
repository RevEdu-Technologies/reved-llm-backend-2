"""Async cache protocol with in-memory and Redis adapters.

The public surface is intentionally tiny (`get`/`set`/`delete`/`clear`) so
call sites stay backend-agnostic. `get_cache()` selects the backend based on
the `CACHE_BACKEND` setting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Generic, Protocol, TypeVar

from app.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncCache(Protocol[T]):
    """Async cache interface supported by all backends."""

    async def get(self, key: str) -> T | None: ...
    async def set(self, key: str, value: T, *, ttl_seconds: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def clear(self) -> None: ...
    async def ping(self) -> bool: ...


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class InMemoryTTLCache(Generic[T]):
    """Thread/async-safe in-memory cache with per-key TTL."""

    def __init__(self, *, default_ttl_seconds: float = 300.0) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be greater than zero.")
        self._default_ttl = default_ttl_seconds
        self._store: dict[str, _CacheEntry[T]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._store.pop(key, None)
                return None
            return entry.value

    async def set(
        self,
        key: str,
        value: T,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            raise ValueError("ttl_seconds must be greater than zero.")
        async with self._lock:
            self._store[key] = _CacheEntry(
                value=value,
                expires_at=time.monotonic() + ttl,
            )

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()

    async def size(self) -> int:
        async with self._lock:
            return len(self._store)

    async def ping(self) -> bool:
        return True


class RedisTTLCache:
    """Redis-backed cache conforming to the AsyncCache protocol.

    Values are serialized with JSON so only JSON-safe payloads (dicts, lists,
    strings, numbers, booleans, None) are supported. That matches how the
    service layer uses the cache today (envelopes and structured dicts).
    """

    def __init__(self, *, redis_url: str, default_ttl_seconds: float = 300.0) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be greater than zero.")
        from redis.asyncio import Redis

        self._default_ttl = default_ttl_seconds
        self._client: Redis = Redis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Cache value for %s is not JSON; returning raw string.", key)
            return raw

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl <= 0:
            raise ValueError("ttl_seconds must be greater than zero.")
        payload = json.dumps(value, default=str)
        await self._client.set(key, payload, ex=int(ttl))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def clear(self) -> None:
        await self._client.flushdb()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False


@lru_cache(maxsize=1)
def get_cache() -> AsyncCache:
    """Return the configured process-wide cache instance."""

    settings = get_settings()
    ttl = float(settings.cache_default_ttl_seconds)
    if settings.cache_backend == "redis":
        assert settings.redis_url, "redis backend requires REDIS_URL"
        return RedisTTLCache(redis_url=settings.redis_url, default_ttl_seconds=ttl)
    return InMemoryTTLCache(default_ttl_seconds=ttl)


__all__ = ["AsyncCache", "InMemoryTTLCache", "RedisTTLCache", "get_cache"]
