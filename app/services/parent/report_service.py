"""Parent — child activity service (DB queries, no LLM).

Joins ``parents.supabase_user_id`` → ``parents.id`` → ``students.parent_id``
to find a parent's children, then aggregates each child's recent
``chat_messages`` (questions asked the student tutor).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import Settings
from app.db.session import session_scope
from app.models.chat import ChatMessage
from app.models.parent import Parent
from app.models.student import Student
from app.schemas.parent import ChildActivityResponse, ChildActivitySummary
from app.services.cache import NS_PARENT_ACTIVITY, cached_call

logger = logging.getLogger(__name__)


class ParentActivityService:
    """Summarise each linked child's recent learning activity."""

    # Same rationale as TeacherProgressService.CACHE_TTL_SECONDS:
    # the dashboard polls and a 60s window keeps the data feeling live
    # while removing repeated joins on chat_messages.
    CACHE_TTL_SECONDS: float = 60.0

    def __init__(self, *, period_days: int = 14) -> None:
        self._period_days = period_days

    @classmethod
    def from_settings(cls, settings: Settings) -> "ParentActivityService":
        return cls()

    async def summarize(self, *, parent_user_id: uuid.UUID | None) -> ChildActivityResponse:
        if parent_user_id is None:
            return ChildActivityResponse(parent_user_id="unknown", children=[])

        async def _loader() -> dict:
            response = await self._compute(parent_user_id=parent_user_id)
            return response.model_dump(mode="json")

        data = await cached_call(
            namespace=NS_PARENT_ACTIVITY,
            identifier=str(parent_user_id),
            ttl_seconds=self.CACHE_TTL_SECONDS,
            loader=_loader,
        )
        return ChildActivityResponse.model_validate(data)

    async def _compute(self, *, parent_user_id: uuid.UUID) -> ChildActivityResponse:
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=self._period_days)

        children_summaries: list[ChildActivitySummary] = []
        try:
            async with session_scope() as session:
                # 1) Resolve parent row by supabase_user_id.
                parent = (
                    await session.execute(
                        select(Parent)
                        .where(Parent.supabase_user_id == parent_user_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if parent is None:
                    logger.info(
                        "No Parent row linked to supabase_user_id=%s — returning empty",
                        parent_user_id,
                    )
                    return ChildActivityResponse(
                        parent_user_id=str(parent_user_id), children=[]
                    )

                # 2) Find children via students.parent_id.
                children = (
                    (
                        await session.execute(
                            select(Student).where(Student.parent_id == parent.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not children:
                    return ChildActivityResponse(
                        parent_user_id=str(parent_user_id), children=[]
                    )

                # 3) For each child, aggregate recent chat_messages.
                for child in children:
                    if child.supabase_user_id is None:
                        # Child has no auth account → no chat_messages yet.
                        children_summaries.append(
                            ChildActivitySummary(
                                student_id=child.id,
                                student_name=child.full_name,
                                grade_level=child.grade_level,
                                period_start=period_start,
                                period_end=period_end,
                                total_questions=0,
                            )
                        )
                        continue

                    rows = (
                        (
                            await session.execute(
                                select(
                                    ChatMessage.subject_hint,
                                    ChatMessage.query,
                                )
                                .where(
                                    ChatMessage.user_id == child.supabase_user_id,
                                    ChatMessage.created_at >= period_start,
                                    ChatMessage.role == "student",
                                )
                                .order_by(ChatMessage.created_at.desc())
                                .limit(200)
                            )
                        ).all()
                    )

                    by_subject: dict[str, int] = {}
                    recent: list[str] = []
                    for subj, query in rows:
                        if subj:
                            by_subject[subj] = by_subject.get(subj, 0) + 1
                        if query and len(recent) < 10:
                            recent.append(query.strip())

                    children_summaries.append(
                        ChildActivitySummary(
                            student_id=child.id,
                            student_name=child.full_name,
                            grade_level=child.grade_level,
                            period_start=period_start,
                            period_end=period_end,
                            total_questions=len(rows),
                            questions_by_subject=dict(
                                sorted(by_subject.items(), key=lambda kv: -kv[1])
                            ),
                            recent_questions=recent,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Parent activity query failed: %s", exc)

        return ChildActivityResponse(
            parent_user_id=str(parent_user_id),
            children=children_summaries,
        )
