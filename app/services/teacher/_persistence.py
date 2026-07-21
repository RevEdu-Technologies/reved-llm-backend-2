"""Shared persistence helpers for AI generations (any role).

Originally scoped to the teacher side; the table was generalised in
``Revision d4f2b9c0e1a3`` so this module now handles student, parent, and
admin generations too. The teacher-specific path keeps its function names
for back-compat; new code should prefer the role-parameterised variants
below.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from app.api._pagination import Cursor, apply_after, encode_cursor
from app.db.session import session_scope
from app.models.ai_generation import AIGeneration

logger = logging.getLogger(__name__)


def _model_dump(payload: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic model or dict to a JSON-safe dict."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return payload


async def persist_generation(
    *,
    user_id: uuid.UUID | None,
    conversation_id: uuid.UUID,
    role: str = "teacher",
    generation_type: str,
    title: str,
    subject: str | None,
    student_class: str | None,
    topic: str | None,
    request_payload: BaseModel | dict[str, Any],
    response_payload: BaseModel | dict[str, Any],
    sources: list[str] | None,
) -> uuid.UUID | None:
    """Insert an ai_generations row. Returns the new id, or None on failure."""

    generation_id = uuid.uuid4()
    try:
        async with session_scope() as session:
            session.add(
                AIGeneration(
                    id=generation_id,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role=role,
                    generation_type=generation_type,
                    subject=subject,
                    student_class=student_class,
                    topic=topic,
                    title=title,
                    request_payload=_model_dump(request_payload),
                    response_payload=_model_dump(response_payload),
                    sources=sources or [],
                )
            )
            # Transactional outbox: the generation.completed event commits in
            # the same transaction as the row, so a subscriber is never told
            # about a generation that didn't persist.
            await _emit_generation_completed(
                session,
                generation_id=generation_id,
                user_id=user_id,
                role=role,
                generation_type=generation_type,
                title=title,
                subject=subject,
            )
        return generation_id
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning("AI generation persistence failed: %s", exc)
        return None


async def _emit_generation_completed(
    session,
    *,
    generation_id: uuid.UUID,
    user_id: uuid.UUID | None,
    role: str,
    generation_type: str,
    title: str,
    subject: str | None,
) -> None:
    """Best-effort outbox write for generation.completed (never blocks the row)."""
    try:
        from app.core.webhooks import EVENT_GENERATION_COMPLETED
        from app.services.webhook_service import WebhookService

        await WebhookService().emit(
            event_type=EVENT_GENERATION_COMPLETED,
            data={
                "generation_id": str(generation_id),
                "user_id": str(user_id) if user_id else None,
                "role": role,
                "generation_type": generation_type,
                "title": title,
                "subject": subject,
            },
            session=session,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook emit (generation.completed) failed: %s", exc)


async def list_generations_for_user(
    *,
    user_id: uuid.UUID | None,
    role: str | None = None,
    limit: int = 50,
    generation_type: str | None = None,
    conversation_id: uuid.UUID | None = None,
    cursor: Cursor | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Return ``(rows, next_cursor)`` for the caller, newest first.

    When ``role`` is supplied, results are scoped to that role's artefacts.
    Pass ``role=None`` only from admin-side aggregations.

    Pagination: pass ``cursor`` decoded from the previous page's
    ``next_cursor`` to fetch the page after it. ``next_cursor`` is non-null
    only when more rows exist past this page.
    """

    if user_id is None:
        return [], None

    try:
        async with session_scope() as session:
            stmt = (
                select(AIGeneration)
                .where(AIGeneration.user_id == user_id)
                .order_by(
                    AIGeneration.created_at.desc(),
                    AIGeneration.id.desc(),
                )
                .limit(limit + 1)  # fetch one extra to detect "more pages"
            )
            if role:
                stmt = stmt.where(AIGeneration.role == role)
            if generation_type:
                stmt = stmt.where(AIGeneration.generation_type == generation_type)
            if conversation_id is not None:
                stmt = stmt.where(AIGeneration.conversation_id == conversation_id)
            if cursor is not None:
                stmt = apply_after(
                    stmt,
                    created_at_col=AIGeneration.created_at,
                    id_col=AIGeneration.id,
                    cursor=cursor,
                )

            rows = (await session.execute(stmt)).scalars().all()
            next_cursor: str | None = None
            if len(rows) > limit:
                # Drop the probe row and emit a cursor pointing at the last
                # kept row.
                rows = rows[:limit]
                last = rows[-1]
                next_cursor = encode_cursor(created_at=last.created_at, id=last.id)
            return (
                [
                    {
                        "generation_id": r.id,
                        "generation_type": r.generation_type,
                        "role": r.role,
                        "title": r.title or "",
                        "subject": r.subject,
                        "student_class": r.student_class,
                        "topic": r.topic,
                        "conversation_id": r.conversation_id,
                        "sources": list(r.sources or []),
                        "created_at": r.created_at,
                    }
                    for r in rows
                ],
                next_cursor,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI generation list query failed: %s", exc)
        return [], None


async def get_generation_for_user(
    *,
    generation_id: uuid.UUID,
    user_id: uuid.UUID | None,
    role: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single generation, gated by owner (and optionally role)."""

    if user_id is None:
        return None

    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(AIGeneration).where(AIGeneration.id == generation_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # Defensive: deny on NULL user_id rows too. A generation
            # without an owner (legacy or batch-ingested) must never be
            # readable through the per-user endpoint — admin-side
            # aggregations should use a separate code path. Mismatched
            # owners are denied with the same "not found" shape so
            # callers cannot probe for valid UUIDs.
            if row.user_id is None or row.user_id != user_id:
                logger.warning(
                    "AIGeneration %s read denied for user %s (owner=%s)",
                    generation_id, user_id, row.user_id,
                )
                return None
            if role and row.role != role:
                # Don't leak existence of cross-role rows owned by the same user.
                return None
            return {
                "generation_id": row.id,
                "generation_type": row.generation_type,
                "role": row.role,
                "title": row.title or "",
                "subject": row.subject,
                "student_class": row.student_class,
                "topic": row.topic,
                "conversation_id": row.conversation_id,
                "sources": list(row.sources or []),
                "request_payload": dict(row.request_payload or {}),
                "response_payload": dict(row.response_payload or {}),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI generation get failed: %s", exc)
        return None
