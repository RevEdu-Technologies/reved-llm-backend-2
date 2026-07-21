"""Webhook subscription + delivery (transactional outbox) ORM models.

``WebhookSubscription`` is the registration: a subscriber URL, its shared
HMAC secret, and the set of event types it wants. ``WebhookDelivery`` is the
outbox/queue row — one per (event × matching subscription). Emitting an event
inserts delivery rows transactionally alongside the domain write so an event
is never lost if the process dies before HTTP delivery; a dispatcher drains
the queue with retry + exponential backoff.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class WebhookSubscription(Base, TimestampMixin):
    __tablename__ = "webhook_subscriptions"
    __table_args__ = (
        # Emit-time lookup: active subscriptions for a school.
        Index(
            "ix_webhook_subscriptions_school_id_is_active",
            "school_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: a NULL school_id is a global subscriber (receives events from
    # every school). Tenant subscribers set their own school_id.
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # List of event-type strings this subscriber wants (subset of
    # app.core.webhooks.ALL_EVENT_TYPES). Stored as JSONB for portability;
    # filtered in Python at emit time.
    event_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WebhookDelivery(Base, TimestampMixin):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # Dispatcher poll: WHERE status='pending' AND next_attempt_at<=now
        # ORDER BY next_attempt_at.
        Index(
            "ix_webhook_deliveries_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_webhook_deliveries_subscription_id_created_at",
            "subscription_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Groups every delivery fanned out from a single emit() call.
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # pending | delivering | delivered | failed
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
