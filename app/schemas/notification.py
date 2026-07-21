"""Notification schemas — cross-role.

Every authenticated user can list and manage their own notifications via
``GET /notifications`` / ``PATCH /notifications/{id}/read``. Admins
additionally have ``POST /admin/notifications`` to deliver a message to
any user.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import UserRole


class NotificationOut(BaseModel):
    """One notification row, suitable for UI rendering."""

    id: uuid.UUID
    recipient_user_id: uuid.UUID
    recipient_role: UserRole
    category: str = Field(
        ...,
        description=(
            "Free-form bucket the UI uses for grouping/icon choice — e.g. "
            "'progress_alert', 'schedule_update', 'system'."
        ),
    )
    title: str
    body: str
    payload: dict[str, Any] | None = None
    is_read: bool = False
    read_at: datetime | None = None
    created_at: datetime


class NotificationListResponse(BaseModel):
    notifications: list[NotificationOut] = Field(default_factory=list)
    unread_count: int = Field(default=0)
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination cursor. Pass it back as ``?cursor=...`` to "
            "fetch the page after these results. ``null`` on the last page."
        ),
    )


class CreateNotificationRequest(BaseModel):
    """Admin payload for delivering a notification to a user."""

    recipient_user_id: uuid.UUID = Field(
        ...,
        description="Supabase auth user_id of the recipient.",
    )
    recipient_role: UserRole = Field(
        ...,
        description="The role context this notification belongs to.",
    )
    category: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1)
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured data for the UI to render (deep-links, counts, etc.).",
    )


class MarkAllReadResponse(BaseModel):
    marked: int = Field(
        ...,
        description="Number of notifications flipped from unread to read.",
    )
