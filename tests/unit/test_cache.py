"""Unit tests for app.services.cache (Phase 5 / N6 cache-aside helper)."""

from __future__ import annotations

import pytest

from app.services import cache as cache_mod
from app.utils.cache import InMemoryTTLCache


def _sample_value(counter, **labels) -> float:
    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryTTLCache:
    """Replace get_cache() with a fresh InMemoryTTLCache so tests don't share state."""

    backend: InMemoryTTLCache = InMemoryTTLCache(default_ttl_seconds=300.0)
    monkeypatch.setattr(cache_mod, "get_cache", lambda: backend)
    return backend


async def test_cached_call_miss_invokes_loader_and_stores(fresh_cache: InMemoryTTLCache):
    calls = 0

    async def loader() -> dict:
        nonlocal calls
        calls += 1
        return {"value": "fresh"}

    result = await cache_mod.cached_call(
        namespace="probe",
        identifier="abc",
        ttl_seconds=60.0,
        loader=loader,
    )
    assert result == {"value": "fresh"}
    assert calls == 1

    # Cache now populated under the canonical key shape.
    assert await fresh_cache.get(cache_mod.make_key("probe", "abc")) == {"value": "fresh"}


async def test_cached_call_hit_skips_loader(fresh_cache: InMemoryTTLCache):
    calls = 0

    async def loader() -> dict:
        nonlocal calls
        calls += 1
        return {"value": "fresh"}

    await cache_mod.cached_call(
        namespace="probe", identifier="abc", ttl_seconds=60.0, loader=loader,
    )
    second = await cache_mod.cached_call(
        namespace="probe", identifier="abc", ttl_seconds=60.0, loader=loader,
    )
    assert second == {"value": "fresh"}
    # Second call was a hit — loader invoked only once.
    assert calls == 1


async def test_cached_call_distinct_identifiers_dont_collide(fresh_cache: InMemoryTTLCache):
    async def make_loader(label: str):
        async def _l():
            return {"value": label}
        return _l

    a = await cache_mod.cached_call(
        namespace="probe", identifier="a", ttl_seconds=60.0, loader=await make_loader("A"),
    )
    b = await cache_mod.cached_call(
        namespace="probe", identifier="b", ttl_seconds=60.0, loader=await make_loader("B"),
    )
    assert a == {"value": "A"}
    assert b == {"value": "B"}


async def test_invalidate_drops_cached_entry(fresh_cache: InMemoryTTLCache):
    calls = 0

    async def loader() -> dict:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    await cache_mod.cached_call(
        namespace="probe", identifier="x", ttl_seconds=60.0, loader=loader,
    )
    await cache_mod.invalidate("probe", "x")

    again = await cache_mod.cached_call(
        namespace="probe", identifier="x", ttl_seconds=60.0, loader=loader,
    )
    assert again == {"calls": 2}
    assert calls == 2


async def test_invalidate_teacher_progress_uses_canonical_key(fresh_cache: InMemoryTTLCache):
    """The named helper must hit the same key shape as cached_call."""

    await fresh_cache.set(
        cache_mod.make_key(cache_mod.NS_TEACHER_PROGRESS, "tid"),
        {"sentinel": True},
    )
    await cache_mod.invalidate_teacher_progress("tid")
    assert await fresh_cache.get(
        cache_mod.make_key(cache_mod.NS_TEACHER_PROGRESS, "tid")
    ) is None


async def test_cached_call_swallows_get_errors_and_invokes_loader(monkeypatch: pytest.MonkeyPatch):
    """A broken cache.get must not break the request — loader runs, value is returned."""

    class BrokenCache:
        async def get(self, key):  # type: ignore[no-untyped-def]
            raise RuntimeError("backend down")

        async def set(self, key, value, *, ttl_seconds=None):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(cache_mod, "get_cache", lambda: BrokenCache())

    async def loader() -> dict:
        return {"ok": True}

    result = await cache_mod.cached_call(
        namespace="probe", identifier="abc", ttl_seconds=60.0, loader=loader,
    )
    assert result == {"ok": True}


async def test_cached_call_swallows_set_errors(monkeypatch: pytest.MonkeyPatch):
    """A broken cache.set must not break the request either."""

    class HalfBrokenCache:
        async def get(self, key):  # type: ignore[no-untyped-def]
            return None

        async def set(self, key, value, *, ttl_seconds=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("write failed")

    monkeypatch.setattr(cache_mod, "get_cache", lambda: HalfBrokenCache())

    async def loader() -> dict:
        return {"ok": True}

    result = await cache_mod.cached_call(
        namespace="probe", identifier="abc", ttl_seconds=60.0, loader=loader,
    )
    assert result == {"ok": True}


async def test_cached_call_records_hit_and_miss_metrics(fresh_cache: InMemoryTTLCache):
    from app.core.metrics import CACHE_EVENTS

    before_miss = _sample_value(CACHE_EVENTS, namespace="probe-metric", outcome="miss")
    before_hit = _sample_value(CACHE_EVENTS, namespace="probe-metric", outcome="hit")

    async def loader() -> dict:
        return {"v": 1}

    # First call → miss.
    await cache_mod.cached_call(
        namespace="probe-metric", identifier="k", ttl_seconds=60.0, loader=loader,
    )
    # Second call → hit.
    await cache_mod.cached_call(
        namespace="probe-metric", identifier="k", ttl_seconds=60.0, loader=loader,
    )

    after_miss = _sample_value(CACHE_EVENTS, namespace="probe-metric", outcome="miss")
    after_hit = _sample_value(CACHE_EVENTS, namespace="probe-metric", outcome="hit")
    assert after_miss == before_miss + 1
    assert after_hit == before_hit + 1


async def test_make_key_shape_is_stable():
    """Key format is part of the contract — invalidation depends on it."""

    assert cache_mod.make_key("teacher-progress", "abc") == "reved:teacher-progress:abc"
    assert cache_mod.make_key(cache_mod.NS_PARENT_ACTIVITY, "u") == "reved:parent-activity:u"
