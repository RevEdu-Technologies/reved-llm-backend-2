"""Webhook subscription management (admin-only).

Admins register subscriber endpoints that receive HMAC-signed event
deliveries (``notification.created``, ``generation.completed``,
``goal.achieved``). Delivery itself runs out-of-band in the dispatcher
(``scripts/webhook_dispatcher.py``); these routes only manage registrations.

The shared secret is returned **once** from ``POST`` and never again.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import get_webhook_service
from app.core.audit import log_auth_event
from app.core.security import AuthenticatedUser, require_role
from app.schemas.common import APIResponse
from app.schemas.webhook import (
    CreateWebhookSubscriptionRequest,
    WebhookSubscriptionCreated,
    WebhookSubscriptionListResponse,
    WebhookSubscriptionOut,
)
from app.services.webhook_service import WebhookService
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _to_out(row) -> WebhookSubscriptionOut:
    return WebhookSubscriptionOut(
        id=row.id,
        url=row.url,
        event_types=list(row.event_types or []),
        school_id=row.school_id,
        description=row.description,
        is_active=row.is_active,
        created_at=row.created_at,
    )


@router.post(
    "/subscriptions",
    response_model=APIResponse[WebhookSubscriptionCreated],
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook subscription (admin)",
    description=(
        "Register a subscriber URL for one or more event types. The response "
        "includes a one-time HMAC `secret` — store it to verify the "
        "`X-RevEd-Signature` header on incoming deliveries. Shown only here."
    ),
)
async def create_subscription(
    body: CreateWebhookSubscriptionRequest,
    service: WebhookService = Depends(get_webhook_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[WebhookSubscriptionCreated]:
    row, secret = await service.register(
        url=body.url,
        event_types=body.event_types,
        school_id=body.school_id,
        description=body.description,
    )
    log_auth_event(
        event="webhook_subscription_create",
        outcome="success",
        user_id=user.user_id,
        role=user.role,
        endpoint="/webhooks/subscriptions",
        extra={"subscription_id": str(row.id), "event_types": body.event_types},
    )
    payload = WebhookSubscriptionCreated(
        **_to_out(row).model_dump(),
        secret=secret,
    )
    return success_response(
        role=user.role,
        data=payload,
        message="Webhook subscription created. Store the secret now — it won't be shown again.",
    )


@router.get(
    "/subscriptions",
    response_model=APIResponse[WebhookSubscriptionListResponse],
    summary="List webhook subscriptions (admin)",
)
async def list_subscriptions(
    include_inactive: bool = False,
    service: WebhookService = Depends(get_webhook_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[WebhookSubscriptionListResponse]:
    rows = await service.list_subscriptions(include_inactive=include_inactive)
    payload = WebhookSubscriptionListResponse(
        subscriptions=[_to_out(r) for r in rows]
    )
    return success_response(
        role=user.role,
        data=payload,
        message=f"{len(rows)} subscription(s).",
    )


@router.delete(
    "/subscriptions/{subscription_id}",
    response_model=APIResponse[None],
    summary="Deactivate a webhook subscription (admin)",
)
async def delete_subscription(
    subscription_id: uuid.UUID,
    service: WebhookService = Depends(get_webhook_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[None]:
    ok = await service.deactivate(subscription_id=subscription_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook subscription not found.",
        )
    log_auth_event(
        event="webhook_subscription_deactivate",
        outcome="success",
        user_id=user.user_id,
        role=user.role,
        endpoint="/webhooks/subscriptions",
        extra={"subscription_id": str(subscription_id)},
    )
    return success_response(
        role=user.role,
        data=None,
        message="Webhook subscription deactivated.",
    )
