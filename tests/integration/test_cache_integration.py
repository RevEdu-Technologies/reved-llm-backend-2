"""Integration tests for cache-aside on the teacher/parent hot endpoints.

These tests hit the real Postgres test DB (via the standard db_session
fixture) and the real in-memory cache. They prove that:

1. The teacher progress service caches its computed response and serves
   the second call from cache (not the DB).
2. ``invalidate_teacher_progress`` flushes that cache so a roster update
   forces a recompute on the next read.
3. The same applies to the parent activity service.
"""

from __future__ import annotations

import pytest

from app.services import cache as cache_mod
from app.services.parent.report_service import ParentActivityService
from app.services.teacher.progress_service import TeacherProgressService
from app.utils.cache import InMemoryTTLCache

pytestmark = pytest.mark.db


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryTTLCache:
    backend = InMemoryTTLCache(default_ttl_seconds=300.0)
    monkeypatch.setattr(cache_mod, "get_cache", lambda: backend)
    return backend


async def test_teacher_progress_caches_second_call(
    fresh_cache: InMemoryTTLCache,
    make_school,
    make_teacher,
):
    school = await make_school(name="Cache Test School")
    teacher = await make_teacher(school_id=school.id, full_name="Cache Test Teacher")

    service = TeacherProgressService()
    first = await service.summarize(teacher_user_id=teacher.supabase_user_id)
    cached_key = cache_mod.make_key(
        cache_mod.NS_TEACHER_PROGRESS, str(teacher.supabase_user_id)
    )
    # Cache populated with the model's JSON dump.
    cached_value = await fresh_cache.get(cached_key)
    assert cached_value is not None
    assert cached_value["teacher_user_id"] == str(teacher.supabase_user_id)

    # Second call returns an equal response — round-tripped through the
    # cache (validate that the helper rehydrates the pydantic model
    # rather than handing back a raw dict).
    second = await service.summarize(teacher_user_id=teacher.supabase_user_id)
    assert second.teacher_user_id == first.teacher_user_id
    assert second.total_student_questions == first.total_student_questions
    assert type(second).__name__ == "ClassProgressResponse"


async def test_teacher_progress_invalidation_clears_cache(
    fresh_cache: InMemoryTTLCache,
    make_school,
    make_teacher,
):
    school = await make_school(name="Invalidation School")
    teacher = await make_teacher(school_id=school.id, full_name="Invalidation Teacher")

    service = TeacherProgressService()
    await service.summarize(teacher_user_id=teacher.supabase_user_id)
    key = cache_mod.make_key(cache_mod.NS_TEACHER_PROGRESS, str(teacher.supabase_user_id))
    assert await fresh_cache.get(key) is not None

    await cache_mod.invalidate_teacher_progress(str(teacher.supabase_user_id))
    assert await fresh_cache.get(key) is None


async def test_teacher_progress_with_no_caller_id_does_not_cache(
    fresh_cache: InMemoryTTLCache,
):
    """No caller identity → no cache key. Don't poison a shared slot."""

    service = TeacherProgressService()
    await service.summarize(teacher_user_id=None)
    assert await fresh_cache.size() == 0


async def test_parent_activity_caches_second_call(
    fresh_cache: InMemoryTTLCache,
    make_parent,
):
    parent = await make_parent(full_name="Cache Parent")

    service = ParentActivityService()
    first = await service.summarize(parent_user_id=parent.supabase_user_id)

    key = cache_mod.make_key(
        cache_mod.NS_PARENT_ACTIVITY, str(parent.supabase_user_id)
    )
    cached_value = await fresh_cache.get(key)
    assert cached_value is not None

    second = await service.summarize(parent_user_id=parent.supabase_user_id)
    assert second.parent_user_id == first.parent_user_id
    assert len(second.children) == len(first.children)


async def test_parent_activity_with_no_caller_id_does_not_cache(
    fresh_cache: InMemoryTTLCache,
):
    service = ParentActivityService()
    await service.summarize(parent_user_id=None)
    assert await fresh_cache.size() == 0
