"""Junction table for ``student ↔ class`` membership.

A student can be in multiple classes (one per subject, typically). The
table is purely associative; no extra metadata beyond ``joined_at``. The
``Teacher.classes`` relationship + this table give us the full picture of
"who's in this teacher's classes" without coarse subject+grade matching.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StudentClassMembership(Base):
    """One row per (student, class) pair. Time-stamped join column."""

    __tablename__ = "student_class_memberships"
    __table_args__ = (
        UniqueConstraint(
            "student_id", "class_id", name="uq_student_class_memberships_pair"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    class_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("classes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
