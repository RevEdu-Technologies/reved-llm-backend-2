"""Teacher Copilot — Class progress summary.

Two scoping modes:

* ``teacher_classes`` — when the calling user is linked to a ``Teacher``
  row, we look up their ``SchoolClass`` rows and filter ``chat_messages``
  to (subject, grade_level) combinations they actually teach.
* ``global_fallback`` — when no Teacher row is linked yet (onboarding
  state), we return the platform-wide aggregate so the dashboard isn't
  blank.

The response carries the scope explicitly so the frontend can label the
view. Per-student mastery and time-on-task remain out of scope for
phase 1 (see ``ClassProgressResponse.note``).
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.db.session import session_scope
from app.models.chat import ChatMessage
from app.models.school import SchoolClass
from app.models.student import Student
from app.models.student_class_membership import StudentClassMembership
from app.models.teacher import Teacher
from app.schemas.teacher import ClassProgressResponse
from app.services.cache import NS_TEACHER_PROGRESS, cached_call

logger = logging.getLogger(__name__)


class TeacherProgressService:
    """Summarise recent class activity for the requesting teacher."""

    # Class progress is a busy dashboard panel — frontends often poll every
    # 30s. A 60s TTL gives the poll cache-hit on every second call without
    # making the data look stale (the underlying ChatMessage window is 14
    # days; the dashboard doesn't notice 60s of lag).
    CACHE_TTL_SECONDS: float = 60.0

    def __init__(self, *, period_days: int = 14) -> None:
        self._period_days = period_days

    @classmethod
    def from_settings(cls, settings: Settings) -> "TeacherProgressService":
        return cls()

    async def summarize(self, *, teacher_user_id: uuid.UUID | None) -> ClassProgressResponse:
        if teacher_user_id is None:
            # No caller identity → no cache key. Run the global-fallback path
            # without caching so we don't poison a shared key.
            return await self._compute(teacher_user_id=None)

        async def _loader() -> dict:
            response = await self._compute(teacher_user_id=teacher_user_id)
            return response.model_dump(mode="json")

        data = await cached_call(
            namespace=NS_TEACHER_PROGRESS,
            identifier=str(teacher_user_id),
            ttl_seconds=self.CACHE_TTL_SECONDS,
            loader=_loader,
        )
        return ClassProgressResponse.model_validate(data)

    async def _compute(self, *, teacher_user_id: uuid.UUID | None) -> ClassProgressResponse:
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=self._period_days)

        questions_by_subject: dict[str, int] = {}
        questions_by_class: dict[str, int] = {}
        total = 0
        previews: list[str] = []
        scope: str = "global_fallback"

        try:
            async with session_scope() as session:
                # 1) Resolve caller's Teacher row → their classes.
                teacher_class_ids: list[uuid.UUID] = []
                teacher_filter_pairs: list[tuple[str | None, str | None]] = []
                if teacher_user_id is not None:
                    teacher_row = (
                        await session.execute(
                            select(Teacher)
                            .where(Teacher.supabase_user_id == teacher_user_id)
                            .options(selectinload(Teacher.classes))
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if teacher_row and teacher_row.classes:
                        teacher_class_ids = [c.id for c in teacher_row.classes]
                        teacher_filter_pairs = [
                            ((c.subject or "").lower() or None, c.grade_level or None)
                            for c in teacher_row.classes
                        ]
                        scope = "teacher_classes"

                # 2) Prefer roster-based filtering when memberships exist.
                #    Look up enrolled students' supabase_user_ids; if any are
                #    found, restrict chat_messages to those users. Otherwise
                #    fall back to (subject, grade_level) pair matching.
                roster_user_ids: list[uuid.UUID] = []
                if teacher_class_ids:
                    rows = (
                        await session.execute(
                            select(Student.supabase_user_id)
                            .join(
                                StudentClassMembership,
                                StudentClassMembership.student_id == Student.id,
                            )
                            .where(
                                StudentClassMembership.class_id.in_(teacher_class_ids),
                                Student.supabase_user_id.is_not(None),
                            )
                            .distinct()
                        )
                    ).all()
                    roster_user_ids = [row[0] for row in rows]

                # 3) Build the chat_messages query.
                stmt = select(
                    ChatMessage.subject_hint,
                    ChatMessage.grade_level,
                    ChatMessage.query,
                ).where(
                    ChatMessage.created_at >= period_start,
                    ChatMessage.role == "student",
                )

                if roster_user_ids:
                    # Roster-based: exact class memberships.
                    stmt = stmt.where(ChatMessage.user_id.in_(roster_user_ids))
                elif teacher_filter_pairs:
                    # Heuristic fallback: (subject, grade_level) pairs.
                    clauses = []
                    for subj, grade in teacher_filter_pairs:
                        parts = []
                        if subj:
                            parts.append(ChatMessage.subject_hint == subj)
                        if grade:
                            parts.append(ChatMessage.grade_level == grade)
                        if not parts:
                            continue
                        if len(parts) == 1:
                            clauses.append(parts[0])
                        else:
                            from sqlalchemy import and_
                            clauses.append(and_(*parts))
                    if clauses:
                        stmt = stmt.where(or_(*clauses))

                stmt = stmt.order_by(ChatMessage.created_at.desc()).limit(500)
                rows = (await session.execute(stmt)).all()
                total = len(rows)
                for subj, klass, query in rows:
                    if subj:
                        questions_by_subject[subj] = questions_by_subject.get(subj, 0) + 1
                    if klass:
                        questions_by_class[klass] = questions_by_class.get(klass, 0) + 1
                    if query and len(previews) < 200:
                        previews.append(query.strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Progress query failed (returning empty stats): %s", exc)

        top_topic_counter: Counter[str] = Counter()
        for q in previews:
            head = q[:80].lower()
            top_topic_counter[head] += 1
        top_topics = [t for t, _ in top_topic_counter.most_common(8)]

        return ClassProgressResponse(
            teacher_user_id=str(teacher_user_id) if teacher_user_id else "unknown",
            period_start=period_start,
            period_end=period_end,
            total_student_questions=total,
            questions_by_subject=dict(
                sorted(questions_by_subject.items(), key=lambda kv: -kv[1])
            ),
            questions_by_class=dict(
                sorted(questions_by_class.items(), key=lambda kv: -kv[1])
            ),
            top_topics=top_topics,
            scope=scope,  # type: ignore[arg-type]
        )
