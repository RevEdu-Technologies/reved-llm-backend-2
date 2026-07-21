"""Webhook subscription schemas (admin-managed).

Admins register subscriber endpoints that receive HMAC-signed event
deliveries. The shared secret is returned **once** at creation time
(``WebhookSubscriptionCreated.secret``) and never again — store it on the
subscriber side to verify the ``X-RevEd-Signature`` header.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.webhooks import ALL_EVENT_TYPES


class CreateWebhookSubscriptionRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="HTTPS endpoint that will receive POSTed event deliveries.",
    )
    event_types: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Event types to receive. Allowed: "
            "notification.created, generation.completed, goal.achieved."
        ),
    )
    school_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Scope deliveries to one school. Omit for a global subscriber "
            "(receives matching events from every school)."
        ),
    )
    description: str | None = Field(default=None, max_length=255)

    @field_validator("url")
    @classmethod
    def _require_http_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("event_types")
    @classmethod
    def _known_event_types(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - ALL_EVENT_TYPES)
        if unknown:
            allowed = ", ".join(sorted(ALL_EVENT_TYPES))
            raise ValueError(
                f"unknown event type(s): {', '.join(unknown)}. Allowed: {allowed}"
            )
        # De-dupe while preserving the caller's order.
        seen: set[str] = set()
        deduped: list[str] = []
        for item in v:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped


class WebhookSubscriptionOut(BaseModel):
    id: uuid.UUID
    url: str
    event_types: list[str]
    school_id: uuid.UUID | None = None
    description: str | None = None
    is_active: bool
    created_at: datetime


class WebhookSubscriptionCreated(WebhookSubscriptionOut):
    """Returned only on creation — carries the one-time shared secret."""

    secret: str = Field(
        ...,
        description=(
            "Shared HMAC secret. Shown once. Store it to verify the "
            "X-RevEd-Signature header on incoming deliveries."
        ),
    )


class WebhookSubscriptionListResponse(BaseModel):
    subscriptions: list[WebhookSubscriptionOut] = Field(default_factory=list)
