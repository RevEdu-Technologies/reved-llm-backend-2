"""add_teacher_generations

Revision ID: b1a8d4c20a3f
Revises: 7c2c14a90e51
Create Date: 2026-05-15 21:00:00.000000

Adds the ``teacher_generations`` table for persisting structured teacher
artefacts (lesson notes, quizzes, student feedback). Each row stores the
full request + response as JSONB so the frontend can re-render any past
generation without re-running the model.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b1a8d4c20a3f"
down_revision: Union[str, None] = "7c2c14a90e51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teacher_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("generation_type", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=True),
        sa.Column("student_class", sa.String(length=32), nullable=True),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_teacher_generations_user_id",
        "teacher_generations",
        ["user_id"],
    )
    op.create_index(
        "ix_teacher_generations_conversation_id",
        "teacher_generations",
        ["conversation_id"],
    )
    op.create_index(
        "ix_teacher_generations_generation_type",
        "teacher_generations",
        ["generation_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_teacher_generations_generation_type", table_name="teacher_generations")
    op.drop_index("ix_teacher_generations_conversation_id", table_name="teacher_generations")
    op.drop_index("ix_teacher_generations_user_id", table_name="teacher_generations")
    op.drop_table("teacher_generations")
