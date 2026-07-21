"""Tests for Supabase JWT verification and role gating."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.security import (
    AuthError,
    AuthenticatedUser,
    AuthorizationError,
    decode_supabase_jwt,
    require_role,
)


class _FakeSettings:
    supabase_jwt_secret = "test-secret"
    supabase_jwt_algorithm = "HS256"
    supabase_jwt_audience = "authenticated"


def _make_token(claims: dict) -> str:
    return jwt.encode(
        claims,
        _FakeSettings.supabase_jwt_secret,
        algorithm=_FakeSettings.supabase_jwt_algorithm,
    )


def test_decode_supabase_jwt_happy_path() -> None:
    uid = uuid.uuid4()
    token = _make_token(
        {
            "sub": str(uid),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
            "email": "stu@example.com",
            "app_metadata": {"role": "teacher"},
        }
    )
    user = decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]
    assert user.user_id == uid
    assert user.role == "teacher"
    assert user.email == "stu@example.com"


def test_decode_supabase_jwt_rejects_expired() -> None:
    token = _make_token(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) - timedelta(minutes=1),
        }
    )
    with pytest.raises(AuthError):
        decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]


def test_decode_supabase_jwt_rejects_bad_signature() -> None:
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
        },
        "wrong-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]


def test_require_role_rejects_mismatch() -> None:
    dep = require_role("teacher")
    user = AuthenticatedUser(
        user_id=uuid.uuid4(), email=None, role="student", is_stub=False
    )
    with pytest.raises(AuthorizationError):
        asyncio.run(dep(user=user))


def test_require_role_allows_match() -> None:
    dep = require_role("student", "teacher")
    user = AuthenticatedUser(
        user_id=uuid.uuid4(), email=None, role="teacher", is_stub=False
    )
    result = asyncio.run(dep(user=user))
    assert result is user
