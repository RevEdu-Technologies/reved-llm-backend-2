"""add_chat_conversation_id

Revision ID: 7c2c14a90e51
Revises: eb4d8cfb75bb
Create Date: 2026-05-15 18:30:00.000000

Adds a ``conversation_id`` UUID column to ``chat_messages`` so the tutor
service can group multiple Q&A turns under one thread. Nullable for
backward compatibility — historical rows without a conversation_id stay
queryable.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7c2c14a90e51"
down_revision: Union[str, None] = "eb4d8cfb75bb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_chat_messages_conversation_id",
        "chat_messages",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
    op.drop_column("chat_messages", "conversation_id")
