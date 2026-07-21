"""Student, Goal, StudyGroup, and PersonalizedAIProfile ORM models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Column,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.school import School


study_group_members = Table(
    "study_group_members",
    Base.metadata,
    Column(
        "group_id",
        UUID(as_uuid=True),
        ForeignKey("study_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "student_id",
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("joined_at", DateTime(timezone=True), nullable=False),
)


class Student(Base, TimestampMixin):
    __tablename__ = "students"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    supabase_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, unique=True, index=True
    )
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    grade_level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    school: Mapped["School | None"] = relationship(back_populates="students")
    goals: Mapped[list["Goal"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    study_groups: Mapped[list["StudyGroup"]] = relationship(
        secondary=study_group_members, back_populates="members"
    )
    ai_profile: Mapped["PersonalizedAIProfile | None"] = relationship(
        back_populates="student", uselist=False, cascade="all, delete-orphan"
    )


class Goal(Base, TimestampMixin):
    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    progress_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    coaching_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    student: Mapped["Student"] = relationship(back_populates="goals")


class StudyGroup(Base, TimestampMixin):
    __tablename__ = "study_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(100), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    student_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)

    members: Mapped[list["Student"]] = relationship(
        secondary=study_group_members, back_populates="study_groups"
    )


class PersonalizedAIProfile(Base, TimestampMixin):
    __tablename__ = "personalized_ai_profiles"
    __table_args__ = (UniqueConstraint("student_id", name="uq_ai_profile_student"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    learning_style: Mapped[str | None] = mapped_column(String(100), nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    strengths: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    weaknesses: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    interests: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_interaction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    student: Mapped["Student"] = relationship(back_populates="ai_profile")
