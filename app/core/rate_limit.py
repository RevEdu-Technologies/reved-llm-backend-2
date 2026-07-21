"""Per-caller rate limiting via slowapi.

The limiter is keyed by the authenticated caller (best-effort) so a single
abusive token cannot drown out everyone else:

1. ``Authorization: Bearer <token>`` → sha256 prefix of the token. We
   intentionally do **not** decode the JWT here — the limiter runs on the
   hot path of every request, and two requests carrying the same token
   share a key for the token's lifetime, which is the property we want.
2. ``X-Dev-Role: <role>`` → ``dev:<role>``. Local dev is single-user;
   bucketing by role is enough to exercise the limiter without per-test
   IP juggling.
3. Fallback → ``ip:<remote_address>``.

A custom 429 handler wraps slowapi's response in the standard RevEd
envelope so the frontend can parse it the same way as any other error
(``{"status":"error","data":{"code":"rate_limited",...}, ...}``).

Storage backend
---------------
* ``REDIS_URL`` set → distributed limits via Redis (production default;
  the docker-compose stack ships a redis service for this).
* Otherwise → in-process memory backend. Fine for local dev and tests,
  unsafe for multi-worker production (each worker has its own counters).
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.config import get_settings
from app.schemas.common import UserRole
from app.utils.response_builder import error_response

logger = logging.getLogger(__name__)


DEFAULT_LIMIT = "60/minute"
"""Per-caller cap applied to every endpoint by default."""

LLM_LIMIT = "10/minute"
"""Tighter cap applied to LLM-backed endpoints (Groq spend control).

This is the ``free``-tier LLM cap and the fallback for any unknown tier.
Paid tiers get a higher cap — see :data:`_DEFAULT_TIER_LLM_LIMITS` and
:func:`llm_limit_for_key`.
"""


# Built-in per-tier LLM caps. Overridable per deploy via the
# ``RATE_LIMIT_LLM_TIERS`` env var (e.g. ``free:10/minute,premium:60/minute``),
# which is layered on top of these defaults — see
# ``app.core.config._get_tier_limit_map``. ``free`` mirrors ``LLM_LIMIT`` so
# the unauthenticated / un-provisioned path is unchanged from before N7.
_DEFAULT_TIER_LLM_LIMITS: dict[str, str] = {
    "free": LLM_LIMIT,
    "basic": "20/minute",
    "premium": "60/minute",
    "unlimited": "1000/minute",
}

_TIER_KEY_SEP = "|"
"""Separates the resolved tier from the per-caller key so the limit
provider can recover the tier from the key string slowapi hands it."""


_ROLE_SEGMENTS = ("student", "teacher", "parent", "admin")


def _role_from_path(path: str) -> UserRole:
    segments = path.lower().split("/")
    for role in _ROLE_SEGMENTS:
        if role in segments:
            return role  # type: ignore[return-value]
    return "system"


def rate_limit_key(request: Request) -> str:
    """Return a stable per-caller key for the limiter."""

    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return "u:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]

    dev_role = request.headers.get("x-dev-role", "").strip().lower()
    if dev_role:
        return f"dev:{dev_role}"

    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _default_tier() -> str:
    """The tier assumed when the caller carries none. Config-driven."""

    try:
        return get_settings().rate_limit_default_tier or "free"
    except Exception:  # noqa: BLE001 — config not ready (import-time / tests)
        return "free"


def tier_llm_limits() -> dict[str, str]:
    """Return the effective ``tier → limit-string`` map.

    Built-in defaults with the ``RATE_LIMIT_LLM_TIERS`` env overrides layered
    on top. Never raises — falls back to the built-in defaults if config is
    unavailable, so a misconfigured env var can't take the limiter offline.
    """

    limits = dict(_DEFAULT_TIER_LLM_LIMITS)
    try:
        overrides = get_settings().rate_limit_llm_tiers
    except Exception:  # noqa: BLE001
        return limits
    for tier, limit in overrides:
        if tier and limit:
            limits[tier.lower()] = limit
    return limits


def resolve_caller_tier(request: Request) -> str:
    """Best-effort subscription tier for the calling request.

    Reads the tier off the authenticated user that ``get_current_user``
    stashed on ``request.state`` — by the time a per-route ``@limiter.limit``
    decorator evaluates, FastAPI has already resolved the auth dependency,
    so the verified-JWT tier (or the dev-stub tier) is available here. Falls
    back to the configured default tier when no user is attached (e.g. an
    endpoint with no auth dependency).
    """

    user = getattr(request.state, "auth_user", None)
    tier = getattr(user, "tier", None)
    if isinstance(tier, str) and tier.strip():
        return tier.strip().lower()
    return _default_tier()


def tiered_rate_limit_key(request: Request) -> str:
    """Per-caller limiter key prefixed with the caller's tier.

    The tier prefix does double duty: it keeps each tier's counters in a
    separate bucket *and* lets :func:`llm_limit_for_key` recover the tier
    (slowapi hands the limit-provider callable only the key string, not the
    request).
    """

    return f"{resolve_caller_tier(request)}{_TIER_KEY_SEP}{rate_limit_key(request)}"


def llm_limit_for_key(key: str) -> str:
    """slowapi dynamic-limit provider: pick the LLM cap for the key's tier.

    ``key`` is whatever :func:`tiered_rate_limit_key` returned, so its tier
    prefix selects the limit. Unknown tiers fall back to the default tier's
    cap (and ultimately to :data:`LLM_LIMIT`).
    """

    tier = key.split(_TIER_KEY_SEP, 1)[0] if _TIER_KEY_SEP in key else _default_tier()
    limits = tier_llm_limits()
    return limits.get(tier) or limits.get(_default_tier()) or LLM_LIMIT


def _resolve_storage_uri() -> str:
    """Pick the limiter's storage backend at import time."""

    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001 — fall through to memory if config not ready
        return "memory://"
    if settings.redis_url:
        return settings.redis_url
    return "memory://"


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[DEFAULT_LIMIT],
    storage_uri=_resolve_storage_uri(),
    # ``headers_enabled=True`` would attach X-RateLimit-* headers to every
    # response, but it requires each decorated route handler to take a
    # ``response: Response`` kwarg so slowapi can mutate it. That's a lot
    # of plumbing for a frontend that doesn't yet consume the headers;
    # the 429 carries Retry-After and is enough for now.
    headers_enabled=False,
    strategy="fixed-window",
)
"""Module-level limiter. Routes import this to apply per-endpoint caps."""


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Translate slowapi's 429 into the standard RevEd envelope."""

    detail = getattr(exc, "detail", None) or str(exc) or "rate limit exceeded"
    role = _role_from_path(request.url.path)
    from app.core.i18n import negotiate_language, translate

    envelope = error_response(
        role=role,
        code="rate_limited",
        message=translate(
            "error.rate_limited",
            negotiate_language(request.headers.get("accept-language")),
            detail=detail,
        ),
    )
    response = JSONResponse(
        status_code=429,
        content=envelope.model_dump(mode="json"),
    )
    response.headers["Retry-After"] = "60"
    logger.info(
        "rate_limited path=%s role=%s key=%s detail=%s",
        request.url.path,
        role,
        rate_limit_key(request),
        detail,
    )
    return response


__all__ = [
    "DEFAULT_LIMIT",
    "LLM_LIMIT",
    "limiter",
    "llm_limit_for_key",
    "rate_limit_exceeded_handler",
    "rate_limit_key",
    "resolve_caller_tier",
    "tier_llm_limits",
    "tiered_rate_limit_key",
]
