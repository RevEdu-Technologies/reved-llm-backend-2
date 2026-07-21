"""Webhook subscription management + outbox dispatch.

Three responsibilities:

* **Registration** — admins register subscriber URLs (``register`` / ``list``
  / ``deactivate``). The shared secret is minted here and returned once.
* **Emit** — domain code calls :meth:`emit` to fan an event out into the
  ``webhook_deliveries`` outbox, one row per matching active subscription.
  ``emit`` can join an existing ``AsyncSession`` so the outbox write commits
  in the same transaction as the domain write (no lost events).
* **Dispatch** — :meth:`deliver_due` claims due rows, POSTs each with an
  HMAC signature, and reschedules failures with exponential backoff until
  ``max_attempts`` is exhausted. Run it from a loop (``scripts/webhook_dispatcher.py``)
  or a scheduler tick.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.webhooks import (
    DEFAULT_MAX_ATTEMPTS,
    DELIVERY_ID_HEADER,
    EVENT_HEADER,
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    backoff_seconds,
    is_success_status,
    secret_token,
    sign_payload,
)
from app.db.session import session_scope
from app.models.webhook import WebhookDelivery, WebhookSubscription

logger = logging.getLogger(__name__)

# (status_code, error_message). status_code is None when the POST never
# completed (timeout / connection error); error_message is None on success.
Sender = Callable[[str, bytes, dict], Awaitable[tuple[int | None, str | None]]]


@dataclass(frozen=True)
class _Claimed:
    """Snapshot of a claimed delivery, detached from the DB session."""

    id: uuid.UUID
    url: str
    secret: str
    event_type: str
    event_id: uuid.UUID
    payload: dict
    attempts: int
    max_attempts: int


@dataclass(frozen=True)
class DispatchResult:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    failed: int = 0


async def _httpx_sender(url: str, body: bytes, headers: dict) -> tuple[int | None, str | None]:
    """Default delivery transport. Returns (status_code, error)."""

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, None
    except Exception as exc:  # noqa: BLE001 — any transport failure is a retryable miss
        return None, f"{type(exc).__name__}: {exc}"


class WebhookService:
    """Manage webhook subscriptions and dispatch the delivery outbox."""

    def __init__(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self._max_attempts = max_attempts

    @classmethod
    def from_settings(cls, settings: Settings) -> "WebhookService":
        return cls()

    # --- Registration ----------------------------------------------------

    async def register(
        self,
        *,
        url: str,
        event_types: list[str],
        school_id: uuid.UUID | None = None,
        description: str | None = None,
    ) -> tuple[WebhookSubscription, str]:
        """Create a subscription. Returns ``(row, plaintext_secret)``.

        The secret is generated here and returned once; it is also stored so
        the dispatcher can sign deliveries. (For an MVP we store it in
        plaintext — a hardening follow-up would envelope-encrypt it at rest.)
        """

        secret = secret_token()
        async with session_scope() as session:
            row = WebhookSubscription(
                id=uuid.uuid4(),
                school_id=school_id,
                url=url,
                secret=secret,
                event_types=list(event_types),
                description=description,
                is_active=True,
            )
            session.add(row)
            await session.flush()
            session.expunge(row)
        return row, secret

    async def list_subscriptions(
        self, *, school_id: uuid.UUID | None = None, include_inactive: bool = False
    ) -> list[WebhookSubscription]:
        """List subscriptions, optionally scoped to a school."""

        async with session_scope() as session:
            stmt = select(WebhookSubscription).order_by(
                WebhookSubscription.created_at.desc()
            )
            if school_id is not None:
                stmt = stmt.where(WebhookSubscription.school_id == school_id)
            if not include_inactive:
                stmt = stmt.where(WebhookSubscription.is_active.is_(True))
            rows = list((await session.execute(stmt)).scalars().all())
            for r in rows:
                session.expunge(r)
            return rows

    async def deactivate(self, *, subscription_id: uuid.UUID) -> bool:
        """Soft-delete a subscription (stops future deliveries). Idempotent."""

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id == subscription_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.is_active = False
            return True

    # --- Emit ------------------------------------------------------------

    async def emit(
        self,
        *,
        event_type: str,
        data: dict,
        school_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
        now: datetime | None = None,
    ) -> int:
        """Fan ``event_type`` out into the outbox. Returns deliveries created.

        Matching subscriptions are active subs that requested ``event_type``
        and are either global (``school_id IS NULL``) or scoped to the
        event's ``school_id``. Pass ``session`` to enlist the outbox writes
        in the caller's transaction (the caller commits); omit it to commit
        in a standalone transaction.
        """

        now = now or datetime.now(timezone.utc)
        if session is not None:
            return await self._emit_in_session(
                session, event_type=event_type, data=data, school_id=school_id, now=now
            )
        async with session_scope() as own_session:
            return await self._emit_in_session(
                own_session,
                event_type=event_type,
                data=data,
                school_id=school_id,
                now=now,
            )

    async def _emit_in_session(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        data: dict,
        school_id: uuid.UUID | None,
        now: datetime,
    ) -> int:
        subs = await self._matching_subscriptions(
            session, event_type=event_type, school_id=school_id
        )
        if not subs:
            return 0
        event_id = uuid.uuid4()
        created = 0
        for sub in subs:
            delivery_id = uuid.uuid4()
            envelope = {
                "id": str(delivery_id),
                "event_id": str(event_id),
                "event_type": event_type,
                "occurred_at": now.isoformat(),
                "data": data,
            }
            session.add(
                WebhookDelivery(
                    id=delivery_id,
                    subscription_id=sub.id,
                    event_id=event_id,
                    event_type=event_type,
                    payload=envelope,
                    status="pending",
                    attempts=0,
                    max_attempts=self._max_attempts,
                    next_attempt_at=now,
                )
            )
            created += 1
        await session.flush()
        logger.info(
            "webhook_emit event_type=%s school_id=%s deliveries=%d",
            event_type,
            school_id,
            created,
        )
        return created

    async def _matching_subscriptions(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        school_id: uuid.UUID | None,
    ) -> list[WebhookSubscription]:
        stmt = select(WebhookSubscription).where(
            WebhookSubscription.is_active.is_(True)
        )
        if school_id is not None:
            # School-scoped subs for this school + global (NULL) subs.
            stmt = stmt.where(
                (WebhookSubscription.school_id == school_id)
                | (WebhookSubscription.school_id.is_(None))
            )
        else:
            # No school context → only global subscribers.
            stmt = stmt.where(WebhookSubscription.school_id.is_(None))
        rows = (await session.execute(stmt)).scalars().all()
        # event_types is JSONB; filter membership in Python.
        return [r for r in rows if event_type in (r.event_types or [])]

    # --- Dispatch --------------------------------------------------------

    async def deliver_due(
        self,
        *,
        limit: int = 20,
        now: datetime | None = None,
        sender: Sender | None = None,
    ) -> DispatchResult:
        """Claim due deliveries, POST each, reschedule/expire on failure."""

        now = now or datetime.now(timezone.utc)
        sender = sender or _httpx_sender

        claimed = await self._claim(limit=limit, now=now)
        if not claimed:
            return DispatchResult()

        delivered = retried = failed = 0
        for item in claimed:
            body = json.dumps(item.payload, separators=(",", ":")).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                EVENT_HEADER: item.event_type,
                EVENT_ID_HEADER: str(item.event_id),
                DELIVERY_ID_HEADER: str(item.id),
                SIGNATURE_HEADER: sign_payload(item.secret, body),
            }
            status_code, error = await sender(item.url, body, headers)
            success = status_code is not None and is_success_status(status_code)
            outcome = await self._finalize(
                item, status_code=status_code, error=error, success=success
            )
            if outcome == "delivered":
                delivered += 1
            elif outcome == "failed":
                failed += 1
            else:
                retried += 1

        return DispatchResult(
            claimed=len(claimed), delivered=delivered, retried=retried, failed=failed
        )

    async def _claim(self, *, limit: int, now: datetime) -> list[_Claimed]:
        """Lock + mark due pending rows as 'delivering'; return snapshots."""

        async with session_scope() as session:
            stmt = (
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return []

            sub_ids = {r.subscription_id for r in rows}
            subs = (
                await session.execute(
                    select(WebhookSubscription).where(
                        WebhookSubscription.id.in_(sub_ids)
                    )
                )
            ).scalars().all()
            sub_map = {s.id: s for s in subs}

            claimed: list[_Claimed] = []
            for r in rows:
                sub = sub_map.get(r.subscription_id)
                if sub is None:
                    # Subscription deleted out from under us — drop the row.
                    r.status = "failed"
                    r.last_error = "subscription_not_found"
                    continue
                r.status = "delivering"
                claimed.append(
                    _Claimed(
                        id=r.id,
                        url=sub.url,
                        secret=sub.secret,
                        event_type=r.event_type,
                        event_id=r.event_id,
                        payload=r.payload,
                        attempts=r.attempts,
                        max_attempts=r.max_attempts,
                    )
                )
            return claimed

    async def _finalize(
        self,
        item: _Claimed,
        *,
        status_code: int | None,
        error: str | None,
        success: bool,
    ) -> str:
        """Persist the result of one delivery attempt. Returns the outcome."""

        now = datetime.now(timezone.utc)
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(WebhookDelivery).where(WebhookDelivery.id == item.id)
                )
            ).scalar_one_or_none()
            if row is None:
                return "failed"

            if success:
                row.status = "delivered"
                row.delivered_at = now
                row.last_status_code = status_code
                row.last_error = None
                return "delivered"

            row.attempts = item.attempts + 1
            row.last_status_code = status_code
            row.last_error = error or (
                f"http_{status_code}" if status_code is not None else "no_response"
            )
            if row.attempts >= item.max_attempts:
                row.status = "failed"
                return "failed"
            row.status = "pending"
            row.next_attempt_at = now + _timedelta_seconds(
                backoff_seconds(row.attempts)
            )
            return "retried"


def _timedelta_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


__all__ = ["DispatchResult", "WebhookService"]
