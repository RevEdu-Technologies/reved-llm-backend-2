"""add_hot_read_composite_indexes

Revision ID: f5a3b8c91d20
Revises: d4f2b9c0e1a3
Create Date: 2026-05-17 22:00:00.000000

Phase 5 / N5 — DB index review. Adds composite indexes that cover the
hot read paths surfaced by service-layer query inspection:

* ``ix_chat_messages_conversation_id_created_at`` — conversation history
  fetch (tutor service, parent/teacher activity drill-down).
* ``ix_chat_messages_user_id_created_at`` — parent ``child-activity`` and
  teacher ``class-progress`` aggregations (``WHERE user_id IN (...)`` +
  ``ORDER BY created_at DESC LIMIT ...``).
* ``ix_ai_generations_user_id_role_created_at`` — per-user list endpoints
  for student / teacher / parent generation history.
* ``ix_notifications_recipient_user_id_created_at`` — notification list
  endpoint (newest-first).

Existing single-column indexes are left in place. PostgreSQL's planner
will pick the composite when ``ORDER BY created_at`` is present and fall
back to the single-column index for pure-equality probes (e.g. existence
checks). Disk cost is small; the alternative — dropping the single-column
indexes — would be a riskier online migration with no measurable benefit
at our row counts.

Uses ``CREATE INDEX CONCURRENTLY`` so the migration is safe to apply
against a populated table without blocking writes. Requires the
migration to run **outside** a transaction; the ``autocommit_block``
context-manager handles that.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "f5a3b8c91d20"
down_revision: Union[str, None] = "d4f2b9c0e1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_chat_messages_conversation_id_created_at",
        "chat_messages",
        ("conversation_id", "created_at"),
    ),
    (
        "ix_chat_messages_user_id_created_at",
        "chat_messages",
        ("user_id", "created_at"),
    ),
    (
        "ix_ai_generations_user_id_role_created_at",
        "ai_generations",
        ("user_id", "role", "created_at"),
    ),
    (
        "ix_notifications_recipient_user_id_created_at",
        "notifications",
        ("recipient_user_id", "created_at"),
    ),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table_name, cols in _NEW_INDEXES:
            op.create_index(
                index_name,
                table_name,
                list(cols),
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table_name, _cols in reversed(_NEW_INDEXES):
            op.drop_index(
                index_name,
                table_name=table_name,
                postgresql_concurrently=True,
                if_exists=True,
            )
