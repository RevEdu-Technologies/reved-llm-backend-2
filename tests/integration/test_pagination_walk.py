"""Integration tests for cursor pagination (Phase 5 / N9).

Walks the AI-generation and notification list endpoints page-by-page
and asserts that:

* Every row is visited exactly once (no duplicates, no skips).
* Rows are returned newest-first.
* ``next_cursor`` becomes ``None`` on the final page.
* The composite-index path from N5 is exercised — the queries use
  ``(created_at, id) < (cursor.c, cursor.i)``.

The walk uses small page sizes against a small seeded dataset so the
test stays fast; the plan's "10k rows without latency degradation"
target is for staging, not the local Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api._pagination import decode_cursor
from app.services.notification_service import NotificationService
from app.services.teacher._persistence import list_generations_for_user

pytestmark = pytest.mark.db


async def _seed_generations(make_ai_generation, *, user_id: uuid.UUID, n: int) -> list[uuid.UUID]:
    """Create n generations for one user with distinct created_at timestamps."""
    base = datetime.now(timezone.utc) - timedelta(hours=n)
    ids: list[uuid.UUID] = []
    for i in range(n):
        row = await make_ai_generation(
            user_id=user_id,
            role="teacher",
            generation_type="lesson_notes",
            title=f"Gen {i}",
            created_at=base + timedelta(minutes=i),
        )
        ids.append(row.id)
    # Newest first → reverse.
    return list(reversed(ids))


async def test_generation_walk_visits_every_row_exactly_once(make_ai_generation):
    user_id = uuid.uuid4()
    expected = await _seed_generations(make_ai_generation, user_id=user_id, n=11)

    page_size = 4
    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0

    while True:
        rows, next_cursor = await list_generations_for_user(
            user_id=user_id,
            role="teacher",
            limit=page_size,
            cursor=decode_cursor(cursor) if cursor else None,
        )
        seen.extend(r["generation_id"] for r in rows)
        pages += 1
        if next_cursor is None:
            break
        cursor = next_cursor
        # Defensive: prevent runaway loop if cursor bug regresses.
        assert pages < 20, "pagination did not terminate"

    assert seen == expected, "rows must be newest-first and visited once each"
    # 11 rows / page_size=4 → pages 1..3 (4 + 4 + 3 rows). The probe-row
    # logic emits next_cursor only when more rows exist, so page 3 ends
    # with next_cursor=None.
    assert pages == 3


async def test_generation_walk_terminates_on_empty_page(make_ai_generation):
    """A user with no rows should immediately end the walk."""

    rows, next_cursor = await list_generations_for_user(
        user_id=uuid.uuid4(),
        role="teacher",
        limit=10,
        cursor=None,
    )
    assert rows == []
    assert next_cursor is None


async def test_generation_walk_respects_role_filter(make_ai_generation):
    """The cursor filter and the role filter compose — pages stay role-scoped."""

    user_id = uuid.uuid4()
    base = datetime.now(timezone.utc) - timedelta(hours=10)
    for i in range(6):
        await make_ai_generation(
            user_id=user_id,
            role="teacher" if i % 2 == 0 else "student",
            generation_type="lesson_notes" if i % 2 == 0 else "learning_path",
            title=f"Gen {i}",
            created_at=base + timedelta(minutes=i),
        )

    rows_teacher, _ = await list_generations_for_user(
        user_id=user_id, role="teacher", limit=50, cursor=None
    )
    rows_student, _ = await list_generations_for_user(
        user_id=user_id, role="student", limit=50, cursor=None
    )
    assert all(r["role"] == "teacher" for r in rows_teacher)
    assert all(r["role"] == "student" for r in rows_student)
    assert len(rows_teacher) == 3
    assert len(rows_student) == 3


async def test_generation_exact_page_size_no_extra_cursor(make_ai_generation):
    """If total rows == page size, next_cursor must be None (no spurious page)."""

    user_id = uuid.uuid4()
    await _seed_generations(make_ai_generation, user_id=user_id, n=4)

    rows, next_cursor = await list_generations_for_user(
        user_id=user_id, role="teacher", limit=4, cursor=None
    )
    assert len(rows) == 4
    assert next_cursor is None


async def test_notification_walk_visits_every_row_exactly_once(make_parent, make_notification):
    """Same walk pattern, applied to NotificationService.list_for_user."""

    parent = await make_parent(full_name="Pagination Parent")
    base = datetime.now(timezone.utc) - timedelta(hours=7)
    expected_ids: list[uuid.UUID] = []
    for i in range(7):
        n = await make_notification(
            recipient_user_id=parent.supabase_user_id,
            recipient_role="parent",
            category="system",
            title=f"N {i}",
            body=f"body {i}",
            created_at=base + timedelta(minutes=i),
        )
        expected_ids.append(n.id)
    expected_ids = list(reversed(expected_ids))  # newest first

    service = NotificationService()
    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        rows, _unread, next_cursor = await service.list_for_user(
            user_id=parent.supabase_user_id,
            limit=3,
            cursor=decode_cursor(cursor) if cursor else None,
        )
        seen.extend(r.id for r in rows)
        pages += 1
        if next_cursor is None:
            break
        cursor = next_cursor
        assert pages < 20

    assert seen == expected_ids
    # 7 rows / page=3 → pages of (3, 3, 1).
    assert pages == 3
