"""Resource-ownership helpers for student-side services.

The student endpoints accept resource identifiers (``student_id``,
``goal_id``, ``group_id``) and historically trusted whatever the body or
URL contained. That trust is unsafe: any authenticated student could read
or mutate another student's resources by guessing or learning a UUID.

These helpers close that gap by mapping a caller's Supabase identity
(``user_id`` from the JWT) to their ``Student.id`` row and checking that
the resource being acted on belongs to that student. Callers should use
``NotFoundError`` (404) for mismatches so attackers cannot tell the
difference between "doesn't exist" and "exists but not yours."
"""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.session import session_scope
from app.models.student import Goal, Student

logger = logging.getLogger(__name__)


async def resolve_student_id_for_user(
    caller_user_id: uuid.UUID | None,
    *,
    session: AsyncSession | None = None,
) -> uuid.UUID | None:
    """Return ``Student.id`` for the calling Supabase user, or ``None``.

    Returns ``None`` when no Student row has been provisioned for the
    caller yet (callers should treat this as 'no resources owned').
    Callers are expected to enforce their own policy on missing rows
    (404 for reads; 422 with onboarding hint for creates).
    """

    if caller_user_id is None:
        return None

    stmt = select(Student.id).where(Student.supabase_user_id == caller_user_id)

    async def _query(s: AsyncSession) -> uuid.UUID | None:
        return await s.scalar(stmt)

    if session is not None:
        return await _query(session)
    async with session_scope() as owned:
        return await _query(owned)


async def assert_student_id_matches_caller(
    *,
    target_student_id: str | uuid.UUID,
    caller_user_id: uuid.UUID | None,
) -> uuid.UUID:
    """Verify ``target_student_id`` belongs to ``caller_user_id``.

    Raises ``NotFoundError`` (→ 404) on any mismatch — including the
    caller having no Student row at all. Returns the resolved
    ``Student.id`` UUID for callers that want to reuse it.
    """

    resolved = await resolve_student_id_for_user(caller_user_id)
    if resolved is None:
        logger.warning(
            "Student resource access denied: no Student row for caller %s",
            caller_user_id,
        )
        raise NotFoundError("Resource not found.")

    if isinstance(target_student_id, str):
        try:
            target_uuid = uuid.UUID(target_student_id)
        except ValueError:
            raise NotFoundError("Resource not found.")
    else:
        target_uuid = target_student_id

    if resolved != target_uuid:
        logger.warning(
            "Student resource access denied: caller %s (student=%s) tried %s",
            caller_user_id,
            resolved,
            target_uuid,
        )
        raise NotFoundError("Resource not found.")

    return resolved


async def assert_goal_owned_by_caller(
    *,
    goal_id: str | uuid.UUID,
    caller_user_id: uuid.UUID | None,
) -> uuid.UUID:
    """Verify a goal belongs to ``caller_user_id``.

    Raises ``NotFoundError`` for any mismatch (missing caller, missing
    goal, or goal owned by a different student). Returns the goal's
    ``student_id`` for callers that need it.
    """

    if caller_user_id is None:
        raise NotFoundError("Resource not found.")

    if isinstance(goal_id, str):
        try:
            goal_uuid = uuid.UUID(goal_id)
        except ValueError:
            raise NotFoundError("Resource not found.")
    else:
        goal_uuid = goal_id

    async with session_scope() as session:
        student_id = await session.scalar(
            select(Goal.student_id).where(Goal.id == goal_uuid)
        )
        if student_id is None:
            raise NotFoundError("Resource not found.")
        owner_supabase_id = await session.scalar(
            select(Student.supabase_user_id).where(Student.id == student_id)
        )

    if owner_supabase_id is None or owner_supabase_id != caller_user_id:
        logger.warning(
            "Goal %s access denied for caller %s (owner_supabase=%s)",
            goal_uuid,
            caller_user_id,
            owner_supabase_id,
        )
        raise NotFoundError("Resource not found.")

    return student_id


__all__ = [
    "resolve_student_id_for_user",
    "assert_student_id_matches_caller",
    "assert_goal_owned_by_caller",
]
