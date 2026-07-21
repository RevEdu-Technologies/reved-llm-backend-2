"""add_student_class_memberships

Revision ID: c3e0e1f8aa12
Revises: b1a8d4c20a3f
Create Date: 2026-05-16 10:00:00.000000

Adds a many-to-many junction between students and classes so /teacher/
class-progress can scope by actual class rosters instead of (subject,
grade_level) heuristics.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3e0e1f8aa12"
down_revision: Union[str, None] = "b1a8d4c20a3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_class_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("classes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "student_id", "class_id", name="uq_student_class_memberships_pair"
        ),
    )
    op.create_index(
        "ix_student_class_memberships_student_id",
        "student_class_memberships",
        ["student_id"],
    )
    op.create_index(
        "ix_student_class_memberships_class_id",
        "student_class_memberships",
        ["class_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_student_class_memberships_class_id",
        table_name="student_class_memberships",
    )
    op.drop_index(
        "ix_student_class_memberships_student_id",
        table_name="student_class_memberships",
    )
    op.drop_table("student_class_memberships")
