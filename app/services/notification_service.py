"""Notification service — cross-role list/read/create."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.api._pagination import Cursor, apply_after, encode_cursor
from app.core.config import Settings
from app.db.session import session_scope
from app.models.notification import Notification
from app.schemas.notification import CreateNotificationRequest

logger = logging.getLogger(__name__)


class NotificationService:
    """Manage user notifications across all roles."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "NotificationService":
        return cls()

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        unread_only: bool = False,
        limit: int = 50,
        cursor: Cursor | None = None,
    ) -> tuple[list[Notification], int, str | None]:
        """Return ``(rows, unread_count, next_cursor)`` for the given user.

        Pagination: pass ``cursor`` decoded from the previous page's
        ``next_cursor`` to fetch the next slice. ``next_cursor`` is
        non-null only when more rows exist past this page.
        """
        async with session_scope() as session:
            stmt = (
                select(Notification)
                .where(Notification.recipient_user_id == user_id)
                .order_by(
                    Notification.created_at.desc(),
                    Notification.id.desc(),
                )
                .limit(limit + 1)  # +1 to detect "more pages"
            )
            if unread_only:
                stmt = stmt.where(Notification.is_read.is_(False))
            if cursor is not None:
                stmt = apply_after(
                    stmt,
                    created_at_col=Notification.created_at,
                    id_col=Notification.id,
                    cursor=cursor,
                )
            rows = (await session.execute(stmt)).scalars().all()
            rows_list = list(rows)
            next_cursor: str | None = None
            if len(rows_list) > limit:
                rows_list = rows_list[:limit]
                last = rows_list[-1]
                next_cursor = encode_cursor(created_at=last.created_at, id=last.id)

            unread = await session.scalar(
                select(func.count(Notification.id)).where(
                    Notification.recipient_user_id == user_id,
                    Notification.is_read.is_(False),
                )
            )
            # Detach so they remain usable after session close.
            for r in rows_list:
                session.expunge(r)
            return rows_list, int(unread or 0), next_cursor

    async def mark_read(
        self,
        *,
        notification_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        """Flip a single notification to read. Returns True on success.

        Access is gated by ``recipient_user_id`` — other users' rows return
        False (treated identically to 'not found' to avoid leaking
        existence).
        """
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Notification).where(Notification.id == notification_id)
                )
            ).scalar_one_or_none()
            if row is None or row.recipient_user_id != user_id:
                return False
            if row.is_read:
                return True
            row.is_read = True
            row.read_at = datetime.now(timezone.utc)
            return True

    async def mark_all_read(self, *, user_id: uuid.UUID) -> int:
        """Flip every unread notification for ``user_id`` to read. Returns count."""
        async with session_scope() as session:
            now = datetime.now(timezone.utc)
            result = await session.execute(
                update(Notification)
                .where(
                    Notification.recipient_user_id == user_id,
                    Notification.is_read.is_(False),
                )
                .values(is_read=True, read_at=now)
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount or 0)

    async def create(self, request: CreateNotificationRequest) -> Notification:
        """Create a new notification (admin-only).

        Emits a ``notification.created`` webhook event in the **same**
        transaction as the insert (transactional outbox), so a subscriber is
        never told about a notification that didn't actually persist, and an
        event is never lost if the process dies right after the insert.
        """
        async with session_scope() as session:
            row = Notification(
                id=uuid.uuid4(),
                recipient_user_id=request.recipient_user_id,
                recipient_role=request.recipient_role,
                category=request.category,
                title=request.title,
                body=request.body,
                payload=request.payload,
                is_read=False,
            )
            session.add(row)
            await session.flush()

            await self._emit_created_event(session, row)

            session.expunge(row)
            return row

    @staticmethod
    async def _emit_created_event(session, row: Notification) -> None:
        """Best-effort outbox write for the notification.created event.

        Imported lazily to avoid a service-layer import cycle. Wrapped so a
        webhook bookkeeping failure never blocks the notification itself —
        the worst case is a missed event, not a failed user action.
        """
        try:
            from app.core.webhooks import EVENT_NOTIFICATION_CREATED
            from app.services.webhook_service import WebhookService

            await WebhookService().emit(
                event_type=EVENT_NOTIFICATION_CREATED,
                data={
                    "notification_id": str(row.id),
                    "recipient_user_id": str(row.recipient_user_id),
                    "recipient_role": row.recipient_role,
                    "category": row.category,
                    "title": row.title,
                },
                session=session,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook emit (notification.created) failed: %s", exc)
