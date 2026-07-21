"""rename_teacher_generations_to_ai_generations

Revision ID: d4f2b9c0e1a3
Revises: c3e0e1f8aa12
Create Date: 2026-05-16 10:30:00.000000

Generalises the generation-persistence table so student-side and parent-side
endpoints can persist artefacts the same way the teacher side does. Adds a
``role`` column (default 'teacher' for existing rows) and renames the table
+ its indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f2b9c0e1a3"
down_revision: Union[str, None] = "c3e0e1f8aa12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) Add the new role column with default 'teacher' so existing rows are
    #    preserved with their original semantics.
    op.add_column(
        "teacher_generations",
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'teacher'"),
        ),
    )
    op.create_index(
        "ix_teacher_generations_role",
        "teacher_generations",
        ["role"],
    )

    # 2) Rename the table + drop the auto-named PK constraint's index suffix
    #    by recreating it implicitly (PG renames the PK constraint with the
    #    table). We rename indexes too so future Alembic autogen sees them.
    op.rename_table("teacher_generations", "ai_generations")
    op.execute("ALTER INDEX ix_teacher_generations_user_id RENAME TO ix_ai_generations_user_id")
    op.execute(
        "ALTER INDEX ix_teacher_generations_conversation_id RENAME TO ix_ai_generations_conversation_id"
    )
    op.execute(
        "ALTER INDEX ix_teacher_generations_generation_type RENAME TO ix_ai_generations_generation_type"
    )
    op.execute("ALTER INDEX ix_teacher_generations_role RENAME TO ix_ai_generations_role")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_ai_generations_role RENAME TO ix_teacher_generations_role")
    op.execute(
        "ALTER INDEX ix_ai_generations_generation_type RENAME TO ix_teacher_generations_generation_type"
    )
    op.execute(
        "ALTER INDEX ix_ai_generations_conversation_id RENAME TO ix_teacher_generations_conversation_id"
    )
    op.execute("ALTER INDEX ix_ai_generations_user_id RENAME TO ix_teacher_generations_user_id")
    op.rename_table("ai_generations", "teacher_generations")
    op.drop_index("ix_teacher_generations_role", table_name="teacher_generations")
    op.drop_column("teacher_generations", "role")
