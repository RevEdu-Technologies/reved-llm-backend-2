"""In-memory repository interfaces for student features.

These repositories define the persistence contract used by the student
services. The in-memory implementations are the Phase-1 default so features
can be demoed end-to-end without a database. Phase 5 will provide Supabase-
backed replacements that conform to the same interface without touching the
service or route layers.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Literal, Protocol

GoalStatus = Literal["active", "completed"]


@dataclass(slots=True)
class GoalRecord:
    """Persisted representation of a student learning goal."""

    id: str
    student_id: str
    title: str
    description: str | None
    subject: str | None
    target_date: date | None
    progress_percent: int
    status: GoalStatus
    coaching_note: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class StudyGroupRecord:
    """Persisted representation of a collaborative study group."""

    id: str
    name: str
    subject: str
    topic: str
    student_class: str
    creator_student_id: str
    member_student_ids: list[str]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class GoalRepository(Protocol):
    async def create(self, record: GoalRecord) -> GoalRecord: ...
    async def get(self, goal_id: str) -> GoalRecord | None: ...
    async def list_for_student(self, student_id: str) -> list[GoalRecord]: ...
    async def update(self, record: GoalRecord) -> GoalRecord: ...


class StudyGroupRepository(Protocol):
    async def create(self, record: StudyGroupRecord) -> StudyGroupRecord: ...
    async def get(self, group_id: str) -> StudyGroupRecord | None: ...
    async def list(
        self,
        *,
        student_class: str | None = None,
        subject: str | None = None,
    ) -> list[StudyGroupRecord]: ...
    async def update(self, record: StudyGroupRecord) -> StudyGroupRecord: ...


class InMemoryGoalRepository:
    """Phase-1 in-memory store for student goals."""

    def __init__(self) -> None:
        self._store: dict[str, GoalRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: GoalRecord) -> GoalRecord:
        async with self._lock:
            self._store[record.id] = record
            return record

    async def get(self, goal_id: str) -> GoalRecord | None:
        async with self._lock:
            return self._store.get(goal_id)

    async def list_for_student(self, student_id: str) -> list[GoalRecord]:
        async with self._lock:
            return [r for r in self._store.values() if r.student_id == student_id]

    async def update(self, record: GoalRecord) -> GoalRecord:
        async with self._lock:
            self._store[record.id] = record
            return record


class InMemoryStudyGroupRepository:
    """Phase-1 in-memory store for collaborative study groups."""

    def __init__(self) -> None:
        self._store: dict[str, StudyGroupRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: StudyGroupRecord) -> StudyGroupRecord:
        async with self._lock:
            self._store[record.id] = record
            return record

    async def get(self, group_id: str) -> StudyGroupRecord | None:
        async with self._lock:
            return self._store.get(group_id)

    async def list(
        self,
        *,
        student_class: str | None = None,
        subject: str | None = None,
    ) -> list[StudyGroupRecord]:
        async with self._lock:
            records = list(self._store.values())
        if student_class:
            records = [r for r in records if r.student_class == student_class]
        if subject:
            records = [r for r in records if r.subject == subject]
        return records

    async def update(self, record: StudyGroupRecord) -> StudyGroupRecord:
        async with self._lock:
            self._store[record.id] = record
            return record


__all__ = [
    "GoalRecord",
    "GoalRepository",
    "InMemoryGoalRepository",
    "InMemoryStudyGroupRepository",
    "StudyGroupRecord",
    "StudyGroupRepository",
    "_new_id",
    "_now",
    "replace",
]
