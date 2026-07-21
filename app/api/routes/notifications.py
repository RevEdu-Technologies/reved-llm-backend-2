"""Cross-role notifications API.

These endpoints are usable by any authenticated user (any role). Each
operation is scoped by the caller's ``user_id`` — users can only manage
their own notifications. Admin-side delivery lives separately under
``/admin/notifications``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api._pagination import clamp_limit, decode_cursor
from app.api.dependencies import get_notification_service
from app.core.security import AuthenticatedUser, get_current_user
from app.schemas.common import APIResponse
from app.schemas.notification import (
    MarkAllReadResponse,
    NotificationListResponse,
    NotificationOut,
)
from app.services.notification_service import NotificationService
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
)


@router.get(
    "",
    response_model=APIResponse[NotificationListResponse],
    summary="List the caller's notifications",
    description=(
        "Returns the caller's notifications newest first. Pass "
        "``unread_only=true`` to filter out already-read items. The "
        "``unread_count`` field on the response makes it cheap for the UI "
        "to render a badge without a second call."
    ),
)
async def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(
        default=None,
        description=(
            "Opaque pagination cursor from a prior response's ``next_cursor``. "
            "Omit on the first page."
        ),
    ),
    service: NotificationService = Depends(get_notification_service),
    user: AuthenticatedUser = Depends(get_current_user),
) -> APIResponse[NotificationListResponse]:
    decoded = decode_cursor(cursor) if cursor else None
    rows, unread, next_cursor = await service.list_for_user(
        user_id=user.user_id,
        unread_only=unread_only,
        limit=clamp_limit(limit),
        cursor=decoded,
    )
    payload = NotificationListResponse(
        notifications=[
            NotificationOut(
                id=r.id,
                recipient_user_id=r.recipient_user_id,
                recipient_role=r.recipient_role,  # type: ignore[arg-type]
                category=r.category,
                title=r.title,
                body=r.body,
                payload=r.payload,
                is_read=r.is_read,
                read_at=r.read_at,
                created_at=r.created_at,
            )
            for r in rows
        ],
        unread_count=unread,
        next_cursor=next_cursor,
    )
    return success_response(
        role=user.role,
        data=payload,
        message=f"{len(rows)} notification(s), {unread} unread.",
    )


@router.patch(
    "/{notification_id}/read",
    response_model=APIResponse[NotificationOut],
    summary="Mark a single notification as read",
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    service: NotificationService = Depends(get_notification_service),
    user: AuthenticatedUser = Depends(get_current_user),
) -> APIResponse[NotificationOut]:
    ok = await service.mark_read(
        notification_id=notification_id, user_id=user.user_id
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )
    # Refetch the row so the response shows the updated read_at.
    rows, _, _ = await service.list_for_user(user_id=user.user_id, limit=200)
    target = next((r for r in rows if r.id == notification_id), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found after mark-read.",
        )
    payload = NotificationOut(
        id=target.id,
        recipient_user_id=target.recipient_user_id,
        recipient_role=target.recipient_role,  # type: ignore[arg-type]
        category=target.category,
        title=target.title,
        body=target.body,
        payload=target.payload,
        is_read=target.is_read,
        read_at=target.read_at,
        created_at=target.created_at,
    )
    return success_response(
        role=user.role, data=payload, message="Notification marked read."
    )


@router.patch(
    "/mark-all-read",
    response_model=APIResponse[MarkAllReadResponse],
    summary="Mark every unread notification for the caller as read",
)
async def mark_all_notifications_read(
    service: NotificationService = Depends(get_notification_service),
    user: AuthenticatedUser = Depends(get_current_user),
) -> APIResponse[MarkAllReadResponse]:
    count = await service.mark_all_read(user_id=user.user_id)
    return success_response(
        role=user.role,
        data=MarkAllReadResponse(marked=count),
        message=f"Marked {count} notification(s) as read.",
    )
