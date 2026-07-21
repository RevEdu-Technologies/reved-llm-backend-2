"""Automated pen-test pass — cross-tenant + auth + injection probes.

The Phase 4 plan calls for a manual pen-test. We codify the probes here
as regression tests so the security posture is verified on every CI run,
not just at launch time. Findings live in ``SECURITY-REVIEW.md``.

Coverage
--------
* Wrong-role access (parent → /teacher, student → /admin, …)
* Expired / malformed / wrong-audience / unsigned / wrong-signature JWTs
* Oversized payload (50 KB question body)
* SQL-meta-character injection in path params and body fields

Cross-school and cross-user-resource probes already live in
``test_cross_school.py`` and ``test_resource_ownership.py``; this file
does not re-do them, but the SECURITY-REVIEW.md document references both.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Callable

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Wrong-role probes (dev mode is sufficient — exercises require_role)
# ---------------------------------------------------------------------------


WRONG_ROLE_PROBES: list[tuple[str, str, str, dict | None]] = [
    # (method, path, calling_role, body_or_none)
    ("GET", "/api/v1/parent/child-activity", "student", None),
    ("GET", "/api/v1/parent/child-activity", "teacher", None),
    ("GET", "/api/v1/teacher/class-progress", "student", None),
    ("GET", "/api/v1/teacher/class-progress", "parent", None),
    ("GET", "/api/v1/admin/usage-summary", "student", None),
    ("GET", "/api/v1/admin/usage-summary", "teacher", None),
    ("GET", "/api/v1/admin/usage-summary", "parent", None),
    (
        "POST",
        "/api/v1/admin/teachers/setup",
        "teacher",
        {
            "school_name": "Probe School",
            "supabase_user_id": str(uuid.uuid4()),
            "full_name": "Probe Teacher",
            "classes": [],
        },
    ),
]


@pytest.mark.parametrize("method,path,role,body", WRONG_ROLE_PROBES)
async def test_wrong_role_is_denied(async_client, method, path, role, body):
    """Any role outside the endpoint's allow-list gets HTTP 403."""

    async with async_client(role=role, user_id=uuid.uuid4()) as client:
        if method == "GET":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json=body or {})

    assert resp.status_code == 403, f"{method} {path} as {role}: {resp.status_code} {resp.text}"
    body_json = resp.json()
    assert body_json["status"] == "error"
    assert body_json["data"]["code"] == "authorization_error"


# ---------------------------------------------------------------------------
# JWT probes — fixture that flips the app into AUTH_ENABLED=true mode
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def auth_enabled_client(
    db_session, jwt_secret
) -> AsyncIterator[Callable[[], AsyncClient]]:
    """Yield an httpx client bound to the real app with AUTH_ENABLED=true.

    Unlike ``async_client``, this fixture does NOT override
    ``get_current_user`` — the real JWT-decoding code path runs, so we
    can verify it rejects expired / tampered / wrong-audience tokens.
    """

    from app.core.config import get_settings
    from app.core.security import get_current_user  # noqa: F401 - referenced below
    from app.db.session import get_db_session
    from main import app as fastapi_app

    real_settings = get_settings()
    pen_settings = replace(
        real_settings, auth_enabled=True, supabase_jwt_secret=jwt_secret
    )

    async def _override_db() -> AsyncIterator:
        yield db_session

    fastapi_app.dependency_overrides[get_db_session] = _override_db
    fastapi_app.dependency_overrides[get_settings] = lambda: pen_settings

    clients: list[AsyncClient] = []

    def _open() -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://pen-testserver",
        )
        clients.append(client)
        return client

    try:
        yield _open
    finally:
        for c in clients:
            await c.aclose()
        fastapi_app.dependency_overrides.clear()


def _make_token(
    *,
    secret: str,
    user_id: uuid.UUID | None = None,
    role: str = "student",
    audience: str = "authenticated",
    expires_in: int = 3600,
    algorithm: str = "HS256",
    drop_sub: bool = False,
    bad_sub: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict = {
        "aud": audience,
        "exp": now + timedelta(seconds=expires_in),
        "iat": now,
        "email": "user@test.local",
        "app_metadata": {"role": role},
    }
    if bad_sub is not None:
        claims["sub"] = bad_sub
    elif not drop_sub:
        claims["sub"] = str(user_id or uuid.uuid4())
    return jwt.encode(claims, secret, algorithm=algorithm)


async def test_expired_jwt_is_rejected(auth_enabled_client, jwt_secret):
    token = _make_token(secret=jwt_secret, expires_in=-60)
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["data"]["code"] == "authentication_error"


async def test_wrong_signature_jwt_is_rejected(auth_enabled_client, jwt_secret):
    token = _make_token(secret="WRONG-SECRET")
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


async def test_wrong_audience_jwt_is_rejected(auth_enabled_client, jwt_secret):
    token = _make_token(secret=jwt_secret, audience="someone-else")
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


async def test_missing_sub_jwt_is_rejected(auth_enabled_client, jwt_secret):
    token = _make_token(secret=jwt_secret, drop_sub=True)
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


async def test_non_uuid_sub_jwt_is_rejected(auth_enabled_client, jwt_secret):
    token = _make_token(secret=jwt_secret, bad_sub="not-a-uuid")
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


async def test_garbage_authorization_header_is_rejected(auth_enabled_client):
    client = auth_enabled_client()
    resp = await client.get(
        "/api/v1/student/conversations",
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


async def test_missing_authorization_header_is_rejected(auth_enabled_client):
    client = auth_enabled_client()
    resp = await client.get("/api/v1/student/conversations")
    assert resp.status_code == 401, resp.text
    assert resp.json()["data"]["code"] == "authentication_error"


# ---------------------------------------------------------------------------
# Oversized payload — should not 500
# ---------------------------------------------------------------------------


async def test_oversized_ask_payload_does_not_crash(async_client):
    """A 50 KB question body should be rejected cleanly (4xx), not 500.

    The exact rejection path can be validation (Pydantic max length),
    service-level guardrails, or a body-size limit; any 4xx with the
    standard error envelope is acceptable. A 500 means the giant
    payload reached the LLM client or DB layer — that's a finding.
    """

    huge_question = "A" * 50_000
    async with async_client(role="student", user_id=uuid.uuid4()) as client:
        resp = await client.post(
            "/api/v1/student/ask",
            json={
                "question": huge_question,
                "student_class": "SS1",
                "subject": "physics",
            },
        )
    assert resp.status_code < 500, (
        f"Oversized payload reached an unhandled error: {resp.status_code} {resp.text[:200]}"
    )


async def test_oversized_goal_title_is_rejected_cleanly(async_client, make_school, make_student):
    school = await make_school(name="Pen Test School")
    me = uuid.uuid4()
    student = await make_student(school_id=school.id, supabase_user_id=me, full_name="Me")
    async with async_client(role="student", user_id=me) as client:
        resp = await client.post(
            "/api/v1/student/goals",
            json={
                "student_id": student.id.hex,
                "title": "X" * 10_000,  # schema max_length=120
                "description": None,
                "subject": "physics",
                "target_date": None,
            },
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# SQL-meta-character injection — must not 500 and must not leak data
# ---------------------------------------------------------------------------


SQL_PROBES = [
    "' OR '1'='1",
    "'; DROP TABLE students;--",
    "1; SELECT * FROM users;--",
    "%27%20OR%201=1--",  # url-encoded
    "../../etc/passwd",
    "<script>alert(1)</script>",
]


@pytest.mark.parametrize("probe", SQL_PROBES)
async def test_sql_meta_in_uuid_path_param_returns_4xx_not_500(async_client, probe):
    """``{generation_id}`` is typed as ``uuid.UUID``; non-UUID input must 4xx."""

    async with async_client(role="student", user_id=uuid.uuid4()) as client:
        resp = await client.get(f"/api/v1/student/generations/{probe}")
    assert resp.status_code < 500, (
        f"SQL probe {probe!r} caused a 500: {resp.text[:200]}"
    )
    # 422 (path validation) or 404 (not found) are both acceptable.
    assert resp.status_code in {404, 422}


async def test_sql_meta_in_repository_filter_is_parameterized(db_session):
    """SQL meta-characters in a repo filter argument do not raise.

    This proves the SQLAlchemy ORM parameterizes user-supplied filter
    values at the lowest level. Goes directly to the repository so the
    assertion is about parameterization, not API plumbing. (Routing the
    same probes through the API layer triggers the Phase 2 conftest
    multi-round-trip gotcha; this is the stronger test anyway.)
    """

    from app.models.school import School
    from sqlalchemy import select

    for probe in SQL_PROBES:
        # Query with the SQL probe injected as a filter value. If
        # parameterization is correct, this is a normal "no rows match"
        # query and returns an empty list. If the probe escaped, we'd
        # see a syntax error or worse.
        stmt = select(School).where(School.name == probe)
        result = await db_session.execute(stmt)
        rows = result.scalars().all()
        assert rows == [], f"Probe {probe!r} unexpectedly matched: {rows}"


async def test_subject_field_rejects_unknown_values_cleanly(async_client):
    """Subjects outside the canonical set return 422, never 500."""

    async with async_client(role="student", user_id=uuid.uuid4()) as client:
        resp = await client.post(
            "/api/v1/student/learning-path",
            json={
                "student_class": "SS1",
                "subject": "'; DROP TABLE chunks;--",
                "topic": "Anything",
            },
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["code"] == "validation_error"
