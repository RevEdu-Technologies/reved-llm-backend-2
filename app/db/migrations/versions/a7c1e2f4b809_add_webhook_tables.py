"""add_webhook_tables

Revision ID: a7c1e2f4b809
Revises: f5a3b8c91d20
Create Date: 2026-06-12 10:00:00.000000

Phase 5 / N8 — outbound webhooks / event bus. Two tables:

* ``webhook_subscriptions`` — registered subscriber URLs, their shared HMAC
  secret, and the JSONB list of event types they want. ``school_id`` is
  nullable (NULL = global subscriber).
* ``webhook_deliveries`` — the transactional outbox: one row per
  (event x matching subscription), with a status / attempts / next_attempt_at
  state machine the dispatcher drains with exponential backoff.

Plain (transactional) migration — both tables are new, so there's no online
DDL hazard.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a7c1e2f4b809"
down_revision: Union[str, None] = "f5a3b8c91d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("school_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("event_types", postgresql.JSONB(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_subscriptions"),
    )
    op.create_index(
        "ix_webhook_subscriptions_school_id",
        "webhook_subscriptions",
        ["school_id"],
    )
    op.create_index(
        "ix_webhook_subscriptions_school_id_is_active",
        "webhook_subscriptions",
        ["school_id", "is_active"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            name="fk_webhook_deliveries_subscription_id_webhook_subscriptions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_deliveries"),
    )
    op.create_index(
        "ix_webhook_deliveries_status",
        "webhook_deliveries",
        ["status"],
    )
    op.create_index(
        "ix_webhook_deliveries_status_next_attempt_at",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_subscription_id_created_at",
        "webhook_deliveries",
        ["subscription_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_deliveries_subscription_id_created_at",
        table_name="webhook_deliveries",
    )
    op.drop_index(
        "ix_webhook_deliveries_status_next_attempt_at",
        table_name="webhook_deliveries",
    )
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index(
        "ix_webhook_subscriptions_school_id_is_active",
        table_name="webhook_subscriptions",
    )
    op.drop_index(
        "ix_webhook_subscriptions_school_id",
        table_name="webhook_subscriptions",
    )
    op.drop_table("webhook_subscriptions")
