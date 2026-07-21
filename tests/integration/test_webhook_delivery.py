"""End-to-end webhook outbox: register → emit → dispatch, with retry/backoff.

Runs against the real test DB via the ``db_session`` fixture (which redirects
``session_scope()`` to the test transaction). Delivery is exercised with an
in-test ``sender`` so no real HTTP server is needed; the sender verifies the
HMAC signature exactly as a real subscriber would.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.webhooks import (
    DELIVERY_ID_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    EVENT_GOAL_ACHIEVED,
    EVENT_NOTIFICATION_CREATED,
    verify_signature,
)
from app.models.webhook import WebhookDelivery
from app.schemas.notification import CreateNotificationRequest
from app.services.notification_service import NotificationService
from app.services.webhook_service import WebhookService

pytestmark = pytest.mark.asyncio


def _recording_sender(status_code: int, sink: list[dict]):
    async def _sender(url: str, body: bytes, headers: dict):
        sink.append({"url": url, "body": body, "headers": headers})
        return status_code, None
    return _sender


async def _deliveries_for(db_session, subscription_id) -> list[WebhookDelivery]:
    rows = (
        await db_session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.subscription_id == subscription_id
            )
        )
    ).scalars().all()
    return list(rows)


async def test_emit_creates_one_delivery_per_matching_subscription(db_session, make_school):
    school = await make_school(name="Hook School")
    svc = WebhookService()
    sub1, _ = await svc.register(
        url="https://a.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=school.id,
    )
    sub2, _ = await svc.register(
        url="https://b.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=None,  # global subscriber
    )
    # A third sub that doesn't want this event type — must NOT get a delivery.
    sub3, _ = await svc.register(
        url="https://c.example/hook",
        event_types=[EVENT_GOAL_ACHIEVED],
        school_id=school.id,
    )

    created = await svc.emit(
        event_type=EVENT_NOTIFICATION_CREATED,
        data={"hello": "world"},
        school_id=school.id,
    )
    assert created == 2  # sub1 (school match) + sub2 (global); not sub3

    assert len(await _deliveries_for(db_session, sub1.id)) == 1
    assert len(await _deliveries_for(db_session, sub2.id)) == 1
    assert len(await _deliveries_for(db_session, sub3.id)) == 0


async def test_deliver_signs_payload_and_marks_delivered(db_session, make_school):
    school = await make_school(name="Sign School")
    svc = WebhookService()
    sub, secret = await svc.register(
        url="https://sink.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=school.id,
    )
    await svc.emit(
        event_type=EVENT_NOTIFICATION_CREATED,
        data={"notification_id": str(uuid.uuid4())},
        school_id=school.id,
    )

    sink: list[dict] = []
    result = await svc.deliver_due(sender=_recording_sender(200, sink))

    assert result.claimed == 1
    assert result.delivered == 1
    # Exactly one HTTP call, signature verifies against the body with the secret.
    assert len(sink) == 1
    call = sink[0]
    assert call["url"] == "https://sink.example/hook"
    assert call["headers"][EVENT_HEADER] == EVENT_NOTIFICATION_CREATED
    assert DELIVERY_ID_HEADER in call["headers"]
    assert verify_signature(secret, call["body"], call["headers"][SIGNATURE_HEADER])

    rows = await _deliveries_for(db_session, sub.id)
    assert rows[0].status == "delivered"
    assert rows[0].delivered_at is not None
    assert rows[0].last_status_code == 200


async def test_failed_delivery_is_retried_with_backoff(db_session, make_school):
    school = await make_school(name="Retry School")
    svc = WebhookService()  # default max_attempts (>1)
    sub, _ = await svc.register(
        url="https://down.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=school.id,
    )
    await svc.emit(
        event_type=EVENT_NOTIFICATION_CREATED, data={"x": 1}, school_id=school.id
    )

    sink: list[dict] = []
    before = datetime.now(timezone.utc)
    result = await svc.deliver_due(sender=_recording_sender(500, sink))

    assert result.claimed == 1
    assert result.retried == 1
    assert result.delivered == 0

    row = (await _deliveries_for(db_session, sub.id))[0]
    assert row.status == "pending"  # back in the queue
    assert row.attempts == 1
    assert row.last_status_code == 500
    # Rescheduled into the future (exponential backoff).
    assert row.next_attempt_at > before + timedelta(seconds=1)


async def test_delivery_exhausts_attempts_and_marks_failed(db_session, make_school):
    school = await make_school(name="Dead School")
    svc = WebhookService(max_attempts=1)  # one strike → failed
    sub, _ = await svc.register(
        url="https://gone.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=school.id,
    )
    await svc.emit(
        event_type=EVENT_NOTIFICATION_CREATED, data={"x": 1}, school_id=school.id
    )

    sink: list[dict] = []
    result = await svc.deliver_due(sender=_recording_sender(503, sink))

    assert result.failed == 1
    row = (await _deliveries_for(db_session, sub.id))[0]
    assert row.status == "failed"
    assert row.attempts == 1


async def test_deactivated_subscription_receives_no_new_events(db_session, make_school):
    school = await make_school(name="Quiet School")
    svc = WebhookService()
    sub, _ = await svc.register(
        url="https://x.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=school.id,
    )
    assert await svc.deactivate(subscription_id=sub.id) is True

    created = await svc.emit(
        event_type=EVENT_NOTIFICATION_CREATED, data={"x": 1}, school_id=school.id
    )
    assert created == 0
    assert len(await _deliveries_for(db_session, sub.id)) == 0


async def test_notification_create_emits_webhook_event(db_session, make_school):
    """The transactional-outbox hook in NotificationService.create fires."""

    school = await make_school(name="Notif Hook School")
    svc = WebhookService()
    sub, _ = await svc.register(
        url="https://notif.example/hook",
        event_types=[EVENT_NOTIFICATION_CREATED],
        school_id=None,  # global: notifications carry no school_id context
    )

    recipient = uuid.uuid4()
    await NotificationService().create(
        CreateNotificationRequest(
            recipient_user_id=recipient,
            recipient_role="student",
            category="progress_alert",
            title="Great job!",
            body="You hit your goal.",
        )
    )

    deliveries = await _deliveries_for(db_session, sub.id)
    assert len(deliveries) == 1
    assert deliveries[0].event_type == EVENT_NOTIFICATION_CREATED
    assert deliveries[0].payload["data"]["recipient_user_id"] == str(recipient)
