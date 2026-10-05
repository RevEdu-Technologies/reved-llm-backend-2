"""add_term_week_to_ai_generations

Revision ID: b2d7e5f1a934
Revises: a7c1e2f4b809
Create Date: 2026-10-05 00:00:00.000000

Adds optional ``term`` (1-3) and ``week`` (teaching-week index) columns to
``ai_generations`` so teacher-side generations (lesson notes, generated
content) can be filed under the scheme-of-work week they were produced for.
Both columns are nullable — existing rows and callers that never supply
these fields are unaffected.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2d7e5f1a934"
down_revision: Union[str, None] = "a7c1e2f4b809"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_generations", sa.Column("term", sa.Integer(), nullable=True))
    op.add_column("ai_generations", sa.Column("week", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_generations", "week")
    op.drop_column("ai_generations", "term")
