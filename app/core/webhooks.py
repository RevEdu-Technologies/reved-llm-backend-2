"""Pure helpers for the outbound webhook subsystem (no DB / no I/O).

Kept separate from the service layer so the security-critical bits — HMAC
signing/verification and the retry backoff schedule — are trivially unit
testable and reusable by a subscriber that wants to validate our signature
with the same code path.

Wire contract
-------------
Each delivery is an HTTP ``POST`` with a JSON body and these headers:

* ``X-RevEd-Event``       — the event type (e.g. ``notification.created``).
* ``X-RevEd-Event-Id``    — groups all deliveries fanned out from one emit.
* ``X-RevEd-Delivery-Id`` — this individual delivery attempt's row id.
* ``X-RevEd-Signature``   — ``hmac-sha256=<hexdigest>`` over the **raw body
  bytes**, keyed by the subscription's shared secret.

Subscribers verify by recomputing the HMAC over the received bytes and
comparing in constant time (see :func:`verify_signature`).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# --- Event vocabulary -----------------------------------------------------

EVENT_NOTIFICATION_CREATED = "notification.created"
EVENT_GENERATION_COMPLETED = "generation.completed"
EVENT_GOAL_ACHIEVED = "goal.achieved"

ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_NOTIFICATION_CREATED,
        EVENT_GENERATION_COMPLETED,
        EVENT_GOAL_ACHIEVED,
    }
)

SIGNATURE_HEADER = "X-RevEd-Signature"
EVENT_HEADER = "X-RevEd-Event"
EVENT_ID_HEADER = "X-RevEd-Event-Id"
DELIVERY_ID_HEADER = "X-RevEd-Delivery-Id"

_SIGNATURE_PREFIX = "hmac-sha256="


# --- Retry policy ---------------------------------------------------------

DEFAULT_MAX_ATTEMPTS = 6
"""Total delivery attempts before a delivery is marked permanently failed."""

_BACKOFF_BASE_SECONDS = 10.0
_BACKOFF_CAP_SECONDS = 3600.0


def secret_token(nbytes: int = 32) -> str:
    """Generate a URL-safe shared secret for a new subscription."""

    return secrets.token_urlsafe(nbytes)


def sign_payload(secret: str, body: bytes) -> str:
    """Return ``hmac-sha256=<hexdigest>`` for ``body`` keyed by ``secret``."""

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time check that ``signature`` matches ``body`` under ``secret``.

    Accepts the signature with or without the ``hmac-sha256=`` prefix.
    Returns ``False`` (never raises) for a missing or malformed signature.
    """

    if not signature:
        return False
    candidate = signature.strip()
    if candidate.startswith(_SIGNATURE_PREFIX):
        candidate = candidate[len(_SIGNATURE_PREFIX) :]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate, expected)


def backoff_seconds(
    attempts: int,
    *,
    base: float = _BACKOFF_BASE_SECONDS,
    cap: float = _BACKOFF_CAP_SECONDS,
) -> float:
    """Exponential backoff for the next retry after ``attempts`` failures.

    ``attempts`` is the number of attempts already made (1 after the first
    failure). Delay is ``base * 2**(attempts-1)`` capped at ``cap``. The
    first retry waits ~``base`` seconds; the schedule doubles thereafter.
    """

    if attempts <= 0:
        return base
    delay = base * (2 ** (attempts - 1))
    return min(delay, cap)


def is_success_status(status_code: int) -> bool:
    """A delivery counts as successful on any 2xx response."""

    return 200 <= status_code < 300


__all__ = [
    "ALL_EVENT_TYPES",
    "DEFAULT_MAX_ATTEMPTS",
    "DELIVERY_ID_HEADER",
    "EVENT_GENERATION_COMPLETED",
    "EVENT_GOAL_ACHIEVED",
    "EVENT_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_NOTIFICATION_CREATED",
    "SIGNATURE_HEADER",
    "backoff_seconds",
    "is_success_status",
    "secret_token",
    "sign_payload",
    "verify_signature",
]
