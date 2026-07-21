"""SQLAlchemy-backed repositories for student features.

These implementations conform to the Protocols in ``_repositories`` so they
are a drop-in replacement for the in-memory stores. Services remain
unaware of the backend.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

# Late-bound import: the conftest monkeypatches
# ``app.db.session.get_sessionmaker`` to redirect at the test connection.
# A ``from app.db.session import get_sessionmaker`` captures the original
# lru_cache-wrapped function at import time and bypasses the patch — every
# other service in the repo goes through ``session_scope`` which resolves
# through module globals at call time, so they don't have this problem.
from app.db import session as _db_session
from app.models.student import Goal as GoalModel
from app.models.student import StudyGroup as StudyGroupModel
from app.services.student._repositories import (
    GoalRecord,
    GoalStatus,
    StudyGroupRecord,
)


def _to_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def _goal_to_record(row: GoalModel) -> GoalRecord:
    return GoalRecord(
        id=row.id.hex,
        student_id=row.student_id.hex,
        title=row.title,
        description=row.description,
        subject=row.subject,
        target_date=row.target_date,
        progress_percent=int(row.progress_percent),
        status=row.status,  # type: ignore[arg-type]
        coaching_note=row.coaching_notes or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _group_to_record(row: StudyGroupModel) -> StudyGroupRecord:
    member_ids = [m.id.hex for m in row.members] if row.members else []
    return StudyGroupRecord(
        id=row.id.hex,
        name=row.name,
        subject=row.subject or "",
        topic=row.topic or "",
        student_class=row.student_class or "",
        creator_student_id=row.created_by.hex if row.created_by else "",
        member_student_ids=member_ids,
        created_at=row.created_at,
    )


class SqlGoalRepository:
    """Goal repository persisted in Postgres via SQLAlchemy."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] | None = None
    ) -> None:
        self._session_factory = session_factory or _db_session.get_sessionmaker()

    async def create(self, record: GoalRecord) -> GoalRecord:
        async with self._session_factory() as session:
            row = GoalModel(
                id=_to_uuid(record.id),
                student_id=_to_uuid(record.student_id),
                title=record.title,
                description=record.description,
                subject=record.subject,
                target_date=record.target_date,
                progress_percent=float(record.progress_percent),
                status=record.status,
                coaching_notes=record.coaching_note or None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _goal_to_record(row)

    async def get(self, goal_id: str) -> GoalRecord | None:
        async with self._session_factory() as session:
            row = await session.get(GoalModel, _to_uuid(goal_id))
            return _goal_to_record(row) if row else None

    async def list_for_student(self, student_id: str) -> list[GoalRecord]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(GoalModel)
                .where(GoalModel.student_id == _to_uuid(student_id))
                .order_by(GoalModel.created_at.desc())
            )
            return [_goal_to_record(row) for row in result.scalars().all()]

    async def update(self, record: GoalRecord) -> GoalRecord:
        async with self._session_factory() as session:
            row = await session.get(GoalModel, _to_uuid(record.id))
            if row is None:
                raise LookupError(f"Goal {record.id} not found")
            row.title = record.title
            row.description = record.description
            row.subject = record.subject
            row.target_date = record.target_date
            row.progress_percent = float(record.progress_percent)
            row.status = record.status
            row.coaching_notes = record.coaching_note or None
            await session.commit()
            await session.refresh(row)
            return _goal_to_record(row)


class SqlStudyGroupRepository:
    """Study group repository persisted in Postgres via SQLAlchemy."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] | None = None
    ) -> None:
        self._session_factory = session_factory or _db_session.get_sessionmaker()

    async def create(self, record: StudyGroupRecord) -> StudyGroupRecord:
        async with self._session_factory() as session:
            row = StudyGroupModel(
                id=_to_uuid(record.id),
                name=record.name,
                subject=record.subject or None,
                topic=record.topic or None,
                student_class=record.student_class or None,
                created_by=(
                    _to_uuid(record.creator_student_id)
                    if record.creator_student_id
                    else None
                ),
            )
            session.add(row)
            await session.flush()
            if record.member_student_ids:
                members = await self._fetch_members(session, record.member_student_ids)
                row.members.extend(members)
            await session.commit()
            await session.refresh(row, attribute_names=["members"])
            return _group_to_record(row)

    async def get(self, group_id: str) -> StudyGroupRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(StudyGroupModel)
                .options(selectinload(StudyGroupModel.members))
                .where(StudyGroupModel.id == _to_uuid(group_id))
            )
            row = result.scalar_one_or_none()
            return _group_to_record(row) if row else None

    async def list(
        self,
        *,
        student_class: str | None = None,
        subject: str | None = None,
    ) -> list[StudyGroupRecord]:
        stmt = select(StudyGroupModel).options(selectinload(StudyGroupModel.members))
        if student_class:
            stmt = stmt.where(StudyGroupModel.student_class == student_class)
        if subject:
            stmt = stmt.where(StudyGroupModel.subject == subject)
        stmt = stmt.order_by(StudyGroupModel.created_at.desc())
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [_group_to_record(row) for row in result.scalars().all()]

    async def update(self, record: StudyGroupRecord) -> StudyGroupRecord:
        async with self._session_factory() as session:
            result = await session.execute(
                select(StudyGroupModel)
                .options(selectinload(StudyGroupModel.members))
                .where(StudyGroupModel.id == _to_uuid(record.id))
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise LookupError(f"StudyGroup {record.id} not found")
            row.name = record.name
            row.subject = record.subject or None
            row.topic = record.topic or None
            row.student_class = record.student_class or None
            desired_ids = {_to_uuid(x) for x in record.member_student_ids}
            current_ids = {m.id for m in row.members}
            if desired_ids != current_ids:
                row.members.clear()
                if desired_ids:
                    members = await self._fetch_members(
                        session, [x.hex for x in desired_ids]
                    )
                    row.members.extend(members)
            await session.commit()
            await session.refresh(row, attribute_names=["members"])
            return _group_to_record(row)

    async def _fetch_members(
        self, session: AsyncSession, student_ids: list[str]
    ) -> list:
        from app.models.student import Student as StudentModel

        ids = [_to_uuid(x) for x in student_ids]
        result = await session.execute(
            select(StudentModel).where(StudentModel.id.in_(ids))
        )
        return list(result.scalars().all())


__all__ = ["SqlGoalRepository", "SqlStudyGroupRepository"]
