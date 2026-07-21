"""Supabase JWT verification and FastAPI auth dependencies."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Iterable

import jwt
from fastapi import Depends, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError

from app.core.audit import log_auth_event
from app.core.config import Settings, get_settings
from app.core.errors import RevEdError
from app.schemas.common import UserRole

# Sentinel surfaced on observability lines (access log, audit, future
# tracing) when no auth dependency ran for the request — e.g. a 404 on
# an unrouted path, or a 401 short-circuit before the dependency
# completed. Kept here so the auth vocabulary lives in one module; the
# access-log middleware imports these rather than inventing its own
# string.
ANONYMOUS_USER_ID = "anonymous"
ANONYMOUS_ROLE = "anonymous"


class AuthError(RevEdError):
    """Authentication failure."""

    code = "authentication_error"
    http_status = status.HTTP_401_UNAUTHORIZED


class AuthorizationError(RevEdError):
    """The caller is authenticated but not permitted for this resource."""

    code = "authorization_error"
    http_status = status.HTTP_403_FORBIDDEN


# Default subscription tier when a token carries no tier claim (and the
# value the dev stub adopts unless ``X-Dev-Tier`` overrides it). Tiers
# drive per-caller LLM rate limits — see ``app.core.rate_limit``.
DEFAULT_TIER = "free"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Resolved identity extracted from a Supabase JWT (or a dev stub)."""

    user_id: uuid.UUID
    email: str | None
    role: UserRole
    is_stub: bool = False
    tier: str = DEFAULT_TIER


_STUB_USER = AuthenticatedUser(
    user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    email="dev-stub@reved.local",
    role="student",
    is_stub=True,
    tier=DEFAULT_TIER,
)


# --- OpenAPI security schemes --------------------------------------------
#
# Declaring these here makes Swagger UI surface a clean "Authorize" dialog
# with two inputs: a Bearer token (used in production) and an X-Dev-Role
# header (used in dev when AUTH_ENABLED=false). Without these, custom
# headers wouldn't appear in the per-endpoint UI and devs would have to
# fight Swagger's curl preview to inject them.
_BEARER_SCHEME = HTTPBearer(
    scheme_name="SupabaseBearer",
    description=(
        "Production: paste a Supabase access_token. Ignored when "
        "AUTH_ENABLED=false."
    ),
    auto_error=False,
)
_DEV_ROLE_SCHEME = APIKeyHeader(
    name="X-Dev-Role",
    scheme_name="DevRole",
    description=(
        "Dev only (AUTH_ENABLED=false): set the stub user's role. Accepts "
        "student | teacher | parent | admin. Type just the role string — "
        "no 'Bearer' prefix and no quotes. Ignored in production."
    ),
    auto_error=False,
)
_DEV_TIER_SCHEME = APIKeyHeader(
    name="X-Dev-Tier",
    scheme_name="DevTier",
    description=(
        "Dev only (AUTH_ENABLED=false): set the stub user's subscription "
        "tier (e.g. free | premium | unlimited) to exercise tiered LLM rate "
        "limits locally. Ignored in production, where the tier comes from a "
        "verified JWT claim."
    ),
    auto_error=False,
)


_REVED_ROLES = {"student", "teacher", "parent", "admin"}


def _coerce_reved_role(candidate: object) -> UserRole | None:
    """Return the candidate as a RevEd role if it's one, else None."""

    if isinstance(candidate, str):
        normalized = candidate.strip().lower()
        if normalized in _REVED_ROLES:
            return normalized  # type: ignore[return-value]
    return None


def _role_from_claims(claims: dict[str, object]) -> UserRole:
    """Pick a RevEd role from Supabase JWT claims.

    The RevEd frontend keeps a user's role in the Supabase ``user_roles``
    table (and mirrors it on ``profiles.role``), *not* in the user's
    ``app_metadata``. A plain Supabase access token therefore won't carry
    the RevEd role anywhere this backend can see it — its top-level
    ``role`` claim is always ``"authenticated"``.

    To bridge that without coupling this service to the frontend's
    database, the frontend should register a Supabase **custom access
    token hook** that copies the role from ``user_roles`` into a custom
    claim on the JWT. This function reads, in priority order:

      1. A top-level custom claim — ``user_role`` or ``reved_role``
         (what the access-token hook should set).
      2. ``app_metadata.role`` / ``app_metadata.user_role`` (the
         alternative if you provision roles into app_metadata instead).
      3. ``user_metadata.role`` / ``user_metadata.user_role``.

    Falls back to ``"student"`` (least-privileged) when no RevEd role is
    present. See FRONTEND_INTEGRATION.md §3 for the hook SQL.
    """

    # 1. Top-level custom claims set by a Supabase access-token hook.
    for key in ("user_role", "reved_role"):
        role = _coerce_reved_role(claims.get(key))
        if role is not None:
            return role

    # 2/3. Nested metadata fallbacks.
    for source in ("app_metadata", "user_metadata"):
        meta = claims.get(source)
        if isinstance(meta, dict):
            role = _coerce_reved_role(meta.get("role") or meta.get("user_role"))
            if role is not None:
                return role

    return "student"


def _tier_from_claims(claims: dict[str, object]) -> str:
    """Pick the caller's subscription tier from Supabase JWT claims.

    Mirrors ``_role_from_claims``: the tier lives in ``schools.tier`` in the
    frontend DB and is copied into a custom JWT claim by the same Supabase
    access-token hook that sets the role. We read, in priority order:

      1. Top-level custom claim — ``subscription_tier`` or ``tier``.
      2. ``app_metadata`` / ``user_metadata`` ``subscription_tier`` / ``tier``.

    Falls back to :data:`DEFAULT_TIER` (the most restrictive tier) when no
    tier claim is present, so an un-provisioned token never gets the higher
    paid limits by accident. The tier is advisory — it only widens the LLM
    rate limit; it never grants additional authorization.
    """

    def _coerce(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return None

    for key in ("subscription_tier", "tier"):
        tier = _coerce(claims.get(key))
        if tier is not None:
            return tier

    for source in ("app_metadata", "user_metadata"):
        meta = claims.get(source)
        if isinstance(meta, dict):
            tier = _coerce(meta.get("subscription_tier") or meta.get("tier"))
            if tier is not None:
                return tier

    return DEFAULT_TIER


def decode_supabase_jwt(token: str, settings: Settings | None = None) -> AuthenticatedUser:
    """Verify a Supabase JWT and return the resolved user."""

    settings = settings or get_settings()
    if not settings.supabase_jwt_secret:
        log_auth_event(
            event="jwt_decode", outcome="failure", reason="secret_not_configured"
        )
        raise AuthError("Supabase JWT secret is not configured.")

    try:
        claims = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=[settings.supabase_jwt_algorithm],
            audience=settings.supabase_jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except InvalidTokenError as exc:
        # Map a few common pyjwt failures to stable reason codes so SIEM
        # dashboards can count by category without parsing exc.args.
        exc_name = type(exc).__name__
        reason = {
            "ExpiredSignatureError": "expired",
            "InvalidSignatureError": "invalid_signature",
            "InvalidAudienceError": "invalid_audience",
            "MissingRequiredClaimError": "missing_required_claim",
            "DecodeError": "decode_error",
        }.get(exc_name, "invalid_token")
        log_auth_event(event="jwt_decode", outcome="failure", reason=reason)
        raise AuthError(f"Invalid token: {exc}") from exc

    sub = claims.get("sub")
    if not isinstance(sub, str):
        log_auth_event(event="jwt_decode", outcome="failure", reason="missing_sub")
        raise AuthError("Token is missing a subject claim.")
    try:
        user_id = uuid.UUID(sub)
    except ValueError as exc:
        log_auth_event(event="jwt_decode", outcome="failure", reason="non_uuid_sub")
        raise AuthError("Token subject is not a valid UUID.") from exc

    email = claims.get("email")
    role = _role_from_claims(claims)
    tier = _tier_from_claims(claims)
    # Log successful auth for high-privilege roles only — student-level
    # traffic is the bulk of requests and isn't worth the log volume.
    if role in {"admin", "teacher"}:
        log_auth_event(
            event="jwt_decode",
            outcome="success",
            user_id=user_id,
            role=role,
        )
    return AuthenticatedUser(
        user_id=user_id,
        email=email if isinstance(email, str) else None,
        role=role,
        is_stub=False,
        tier=tier,
    )


_VALID_DEV_ROLES = {"student", "teacher", "parent", "admin"}


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER_SCHEME),
    x_dev_role: str | None = Depends(_DEV_ROLE_SCHEME),
    x_dev_tier: str | None = Depends(_DEV_TIER_SCHEME),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """FastAPI dependency returning the authenticated user.

    Wires both security schemes (``SupabaseBearer`` and ``DevRole``) via
    FastAPI security dependencies so they appear in the Swagger UI
    Authorize dialog. Behavior:

    * ``AUTH_ENABLED=false``: returns a stub user. If ``X-Dev-Role`` is
      one of student/teacher/parent/admin, the stub adopts that role.
    * ``AUTH_ENABLED=true``: requires a valid Supabase Bearer token; the
      ``X-Dev-Role`` header is ignored. Production must not be bypassed
      by a dev convenience.

    Side effect: stashes the resolved user on ``request.state.auth_user``
    so the access-log middleware (see ``app.core.logging``) can include
    ``user_id`` and ``role`` on the per-request line without re-running
    JWT decode. Routes without an auth dependency simply leave
    ``request.state.auth_user`` unset, and the middleware logs
    ``anonymous``.
    """

    if not settings.auth_enabled:
        if x_dev_role:
            normalized = x_dev_role.strip().lower()
            if normalized in _VALID_DEV_ROLES:
                user = replace(_STUB_USER, role=normalized)  # type: ignore[arg-type]
            else:
                user = _STUB_USER
        else:
            user = _STUB_USER
        # Let local dev exercise tiered LLM limits without minting a JWT.
        if x_dev_tier and x_dev_tier.strip():
            user = replace(user, tier=x_dev_tier.strip().lower())
    else:
        if credentials is None or not credentials.credentials:
            raise AuthError("Missing Authorization header.")
        user = decode_supabase_jwt(credentials.credentials, settings=settings)

    request.state.auth_user = user
    return user


def require_role(*roles: UserRole):
    """Return a dependency that enforces one of the given roles."""

    allowed = tuple(roles)

    async def _dependency(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not allowed:
            return user
        if user.role not in allowed:
            log_auth_event(
                event="role_check",
                outcome="failure",
                user_id=user.user_id,
                role=user.role,
                reason="role_mismatch",
                extra={"required": list(allowed)},
            )
            raise AuthorizationError(
                f"This endpoint requires role(s): {', '.join(allowed)}."
            )
        return user

    return _dependency


def require_any_role(roles: Iterable[UserRole]):
    """Alias of require_role for iterables."""

    return require_role(*tuple(roles))
