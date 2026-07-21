"""Structured auth-and-admin audit log.

Every security-relevant event in the API (auth failure, role denial,
admin action) emits one JSON line through this module's dedicated
logger. The format is deliberately flat and machine-greppable so a log
collector can tail stdout and route events to a SIEM without parsing the
default Python log line.

What we log
-----------
* **Auth events**: JWT decode failures (invalid_signature, expired,
  missing_sub, etc.), and successful authentications for high-privilege
  roles (admin, teacher).
* **Role denials**: ``role_mismatch`` raised by ``require_role``.
* **Admin actions**: every mutation through ``/admin/*`` — teacher
  provisioning, parent provisioning, roster updates, notification
  delivery — with the resolved target ids.

What we never log
-----------------
* Raw JWTs, refresh tokens, or any bearer credentials.
* Request bodies in full (only specific extracted ids).
* Passwords (the backend doesn't see them — Supabase owns auth).

Output shape (one JSON object per line)
---------------------------------------
::

    {
        "event":   "jwt_decode" | "role_check" | "admin_action",
        "outcome": "success" | "failure",
        "user_id": "<uuid or null>",
        "role":    "student | teacher | parent | admin | null",
        "endpoint": "<http method + path or null>",
        "reason":  "<short snake_case code or null>",
        "extra":   {"k": "v", ...}        # optional structured fields
    }

Why a dedicated logger
----------------------
The standard ``logging.basicConfig`` prefixes every line with a level +
timestamp + module, which is fine for human reading but noisy for SIEM
ingestion. The ``reved.audit`` logger writes pure JSON to stdout via its
own handler; the rest of the app keeps the human-friendly format.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

_AUDIT_LOGGER_NAME = "reved.audit"


def _build_audit_logger() -> logging.Logger:
    """Create (idempotently) the JSON-only audit logger."""

    logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    if getattr(logger, "_reved_configured", False):
        return logger

    # We emit raw JSON lines, no level prefix or timestamp — the JSON
    # body carries everything a SIEM needs.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Don't double-emit through the root logger.
    logger.propagate = False
    logger._reved_configured = True  # type: ignore[attr-defined]
    return logger


_AUDIT_LOGGER = _build_audit_logger()


def _serializable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    return value


def log_auth_event(
    *,
    event: str,
    outcome: str,
    user_id: uuid.UUID | str | None = None,
    role: str | None = None,
    endpoint: str | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one JSON audit line.

    The function is intentionally synchronous and never raises — audit
    logging must not break the request path. Failures inside this
    function are silently swallowed and a single warning is emitted via
    the standard logger.
    """

    try:
        payload = {
            "event": event,
            "outcome": outcome,
            "user_id": _serializable(user_id),
            "role": role,
            "endpoint": endpoint,
            "reason": reason,
            "extra": _serializable(extra) if extra else None,
        }
        _AUDIT_LOGGER.info(json.dumps(payload, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001 — audit must never break callers
        logging.getLogger(__name__).warning(
            "Audit log emission failed: %s", exc, exc_info=False
        )

    # Mirror the event into the Prometheus counter so the dashboard /
    # alerts can react in near-real-time without a SIEM round-trip.
    # Import here to avoid a circular import at module load.
    try:
        from app.core.metrics import record_auth_event

        record_auth_event(event=event, outcome=outcome, reason=reason)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["log_auth_event"]
