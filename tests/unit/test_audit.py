"""Tests for ``app.core.audit`` JSON event emission and the JWT/role hooks."""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.audit import log_auth_event
from app.core.security import (
    AuthError,
    AuthenticatedUser,
    AuthorizationError,
    decode_supabase_jwt,
    require_role,
)


@pytest.fixture
def audit_stream(monkeypatch):
    """Capture the JSON audit logger's output for inspection."""

    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("reved.audit")
    original_handlers = list(logger.handlers)
    logger.handlers = [handler]
    yield buf
    logger.handlers = original_handlers


def _read_events(buf: io.StringIO) -> list[dict]:
    raw = buf.getvalue().strip().splitlines()
    return [json.loads(line) for line in raw if line]


class _FakeSettings:
    supabase_jwt_secret = "test-audit-secret"
    supabase_jwt_algorithm = "HS256"
    supabase_jwt_audience = "authenticated"


def _make_token(claims: dict, *, secret: str = "test-audit-secret") -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


# --- Direct audit-module tests -------------------------------------------


def test_log_auth_event_emits_one_json_line(audit_stream):
    uid = uuid.uuid4()
    log_auth_event(
        event="role_check",
        outcome="failure",
        user_id=uid,
        role="student",
        endpoint="GET /api/v1/parent/child-activity",
        reason="role_mismatch",
        extra={"required": ["parent", "admin"]},
    )

    events = _read_events(audit_stream)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "role_check"
    assert ev["outcome"] == "failure"
    assert ev["user_id"] == str(uid)
    assert ev["role"] == "student"
    assert ev["endpoint"] == "GET /api/v1/parent/child-activity"
    assert ev["reason"] == "role_mismatch"
    assert ev["extra"] == {"required": ["parent", "admin"]}


def test_log_auth_event_swallows_serialization_failures(audit_stream):
    """An un-serializable ``extra`` value must not break the caller."""

    class _Unserializable:
        pass

    log_auth_event(
        event="admin_action",
        outcome="success",
        extra={"thing": _Unserializable()},
    )
    # No exception — caller is unaffected. The buffer may be empty (the
    # function logs nothing on internal failure), which is the contract.


# --- JWT decode hook tests -----------------------------------------------


def test_jwt_decode_logs_failure_on_expired(audit_stream):
    token = _make_token(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) - timedelta(minutes=5),
        }
    )
    with pytest.raises(AuthError):
        decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]

    events = _read_events(audit_stream)
    assert any(
        e["event"] == "jwt_decode"
        and e["outcome"] == "failure"
        and e["reason"] == "expired"
        for e in events
    ), events


def test_jwt_decode_logs_failure_on_bad_signature(audit_stream):
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

    events = _read_events(audit_stream)
    assert any(
        e["event"] == "jwt_decode"
        and e["outcome"] == "failure"
        and e["reason"] == "invalid_signature"
        for e in events
    ), events


def test_jwt_decode_logs_success_for_teacher_role(audit_stream):
    uid = uuid.uuid4()
    token = _make_token(
        {
            "sub": str(uid),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
            "app_metadata": {"role": "teacher"},
        }
    )
    decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]

    events = _read_events(audit_stream)
    success = [e for e in events if e["outcome"] == "success"]
    assert len(success) == 1
    assert success[0]["role"] == "teacher"
    assert success[0]["user_id"] == str(uid)


def test_jwt_decode_does_not_log_success_for_student(audit_stream):
    """Student traffic dwarfs admin/teacher; avoid log spam."""

    token = _make_token(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
            "app_metadata": {"role": "student"},
        }
    )
    decode_supabase_jwt(token, settings=_FakeSettings())  # type: ignore[arg-type]

    events = _read_events(audit_stream)
    assert all(e["outcome"] != "success" for e in events), events


# --- require_role hook tests ---------------------------------------------


async def test_require_role_logs_role_mismatch(audit_stream):
    dep = require_role("teacher", "admin")
    student = AuthenticatedUser(
        user_id=uuid.uuid4(), email=None, role="student", is_stub=False
    )
    with pytest.raises(AuthorizationError):
        await dep(user=student)

    events = _read_events(audit_stream)
    mismatch = [
        e
        for e in events
        if e["event"] == "role_check" and e["reason"] == "role_mismatch"
    ]
    assert len(mismatch) == 1
    assert mismatch[0]["role"] == "student"
    assert set(mismatch[0]["extra"]["required"]) == {"teacher", "admin"}


async def test_require_role_match_does_not_log(audit_stream):
    """No noise on the happy path."""

    dep = require_role("teacher")
    teacher = AuthenticatedUser(
        user_id=uuid.uuid4(), email=None, role="teacher", is_stub=False
    )
    await dep(user=teacher)
    assert _read_events(audit_stream) == []
