"""Persisted AI artefacts across all roles (student / teacher / parent).

This table generalises the original ``teacher_generations`` table. The
``role`` column discriminates which role produced the artefact, and the
per-role list/detail endpoints filter by it. The shape stays identical so
the frontend can render any generation by inspecting ``response_payload``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AIGeneration(Base, TimestampMixin):
    __tablename__ = "ai_generations"
    __table_args__ = (
        # Per-user list endpoints (teacher/student/parent generation history):
        # WHERE user_id=? AND role=? [AND generation_type=?] ORDER BY created_at DESC.
        Index(
            "ix_ai_generations_user_id_role_created_at",
            "user_id",
            "role",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        doc="Caller role at generation time: student | teacher | parent | admin.",
    )
    generation_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        doc=(
            "Type-tag scoped by role. Teacher: lesson_notes | quiz | "
            "student_feedback. Student: learning_path | career_guidance. "
            "Parent: explain_topic. Admin: (none currently)."
        ),
    )
    subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    student_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sources: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
