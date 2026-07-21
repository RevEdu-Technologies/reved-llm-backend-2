"""Cache-aside helper for hot read endpoints.

A single ``cached_call`` primitive wraps the standard read-cache →
fall-back-to-loader → write-cache flow. Keys are constructed here so
invalidation can target the exact same key shape.

Hit / miss is mirrored into ``reved_cache_events_total`` so the
dashboard can derive a per-namespace hit rate (Phase 5 N6 target:
>60% in staging load tests).

Backend-agnostic: any object implementing :class:`app.utils.cache.AsyncCache`
works. The default ``get_cache()`` selects in-memory or Redis based on the
``CACHE_BACKEND`` setting; this module reaches through that lookup on every
call so tests that monkeypatch ``get_cache`` see fresh state.

Failure mode: cache get/set errors are logged but never raise. A broken
cache degrades to "no cache" — the loader still runs and the caller still
gets an answer.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, TypeVar

from app.core.metrics import record_cache_event
from app.utils.cache import get_cache

logger = logging.getLogger(__name__)

T = TypeVar("T")


# --- Namespaces -----------------------------------------------------------
# Each cache target gets a stable namespace string. The string is the key
# prefix and the Prometheus label, so invalidation and observability stay
# coupled. Add new namespaces here and write a paired ``invalidate_*``
# helper below so call sites never construct keys themselves.

NS_TEACHER_PROGRESS = "teacher-progress"
NS_PARENT_ACTIVITY = "parent-activity"
NS_ADMIN_SCOPE = "admin-scope"


def make_key(namespace: str, identifier: str) -> str:
    """Build the canonical cache key for a (namespace, identifier) pair."""

    return f"reved:{namespace}:{identifier}"


async def cached_call(
    *,
    namespace: str,
    identifier: str,
    ttl_seconds: float | None,
    loader: Callable[[], Awaitable[T]],
) -> T:
    """Read-through cache.

    On hit: return the cached value (already JSON-decoded by the backend).
    On miss: invoke ``loader()``, store its return value under
    ``make_key(namespace, identifier)``, and return it.

    Cache errors are swallowed — on any backend exception the loader is
    invoked and its value returned without being cached. This guarantees
    the cache layer never breaks a request.
    """

    cache = get_cache()
    key = make_key(namespace, identifier)

    try:
        cached = await cache.get(key)
    except Exception as exc:  # noqa: BLE001 - cache must not break callers
        logger.warning("cache.get failed (key=%s): %s", key, exc)
        cached = None

    if cached is not None:
        record_cache_event(namespace=namespace, outcome="hit")
        return cached  # type: ignore[return-value]

    record_cache_event(namespace=namespace, outcome="miss")
    value = await loader()

    try:
        await cache.set(key, value, ttl_seconds=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.set failed (key=%s): %s", key, exc)

    return value


async def invalidate(namespace: str, identifier: str) -> None:
    """Drop the cached entry for one (namespace, id) pair. No-op on miss.

    Swallows cache errors so a flaky Redis can never block a write path.
    """

    cache = get_cache()
    key = make_key(namespace, identifier)
    try:
        await cache.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache.delete failed (key=%s): %s", key, exc)


# --- Namespace-specific shortcuts ----------------------------------------
# Thin wrappers so call sites don't need to import the namespace constant
# separately. Naming convention: ``invalidate_<noun>``.


async def invalidate_teacher_progress(teacher_user_id: str) -> None:
    """Drop a teacher's class-progress cache (e.g. after a roster update)."""

    await invalidate(NS_TEACHER_PROGRESS, teacher_user_id)


async def invalidate_parent_activity(parent_user_id: str) -> None:
    """Drop a parent's child-activity cache."""

    await invalidate(NS_PARENT_ACTIVITY, parent_user_id)


async def invalidate_admin_scope(admin_user_id: str) -> None:
    """Drop the resolved Admin scope cache for one admin user."""

    await invalidate(NS_ADMIN_SCOPE, admin_user_id)


__all__ = [
    "NS_ADMIN_SCOPE",
    "NS_PARENT_ACTIVITY",
    "NS_TEACHER_PROGRESS",
    "cached_call",
    "invalidate",
    "invalidate_admin_scope",
    "invalidate_parent_activity",
    "invalidate_teacher_progress",
    "make_key",
]
