"""Integration tests proving the audit hooks fire end-to-end.

These run a real admin request through the app and capture the JSON
output of the ``reved.audit`` logger. If the wiring breaks (someone
removes a `log_auth_event` call from a route, or the dedicated logger
gets disabled), these go red.
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest


pytestmark = pytest.mark.db


@pytest.fixture
def audit_stream():
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("reved.audit")
    original = list(logger.handlers)
    logger.handlers = [handler]
    yield buf
    logger.handlers = original


def _events_of(buf: io.StringIO, event_name: str) -> list[dict]:
    out = []
    for line in buf.getvalue().strip().splitlines():
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("event") == event_name:
            out.append(payload)
    return out


async def test_admin_teacher_setup_emits_audit_event(
    async_client, audit_stream, make_school, make_admin
):
    school = await make_school(name="Audit Hook School")
    admin_supabase = uuid.uuid4()
    await make_admin(
        school_id=school.id,
        supabase_user_id=admin_supabase,
        full_name="Audit Admin",
        scope="school",
    )

    async with async_client(role="admin", user_id=admin_supabase) as client:
        response = await client.post(
            "/api/v1/admin/teachers/setup",
            json={
                "school_name": school.name,
                "school_country": "NG",
                "supabase_user_id": str(uuid.uuid4()),
                "full_name": "Audited Teacher",
                "email": "audit-teach@test.local",
                "subject_specialty": "physics",
                "classes": [],
            },
        )

    assert response.status_code == 200, response.text
    events = _events_of(audit_stream, "admin_action")
    assert len(events) >= 1
    teacher_setup_events = [
        e for e in events if e["extra"].get("action") == "teacher_setup"
    ]
    assert len(teacher_setup_events) == 1
    ev = teacher_setup_events[0]
    assert ev["outcome"] == "success"
    assert ev["user_id"] == str(admin_supabase)
    assert ev["role"] == "admin"
    assert ev["endpoint"] == "POST /api/v1/admin/teachers/setup"
    # Sensitive — body content (JWT, password) must NOT appear
    assert "password" not in json.dumps(ev).lower()
    assert "bearer" not in json.dumps(ev).lower()


async def test_role_mismatch_at_route_emits_audit_event(
    async_client, audit_stream
):
    """A student calling a parent-only endpoint trips ``require_role``."""

    async with async_client(role="student", user_id=uuid.uuid4()) as client:
        response = await client.get("/api/v1/parent/child-activity")

    assert response.status_code == 403, response.text
    events = _events_of(audit_stream, "role_check")
    mismatches = [e for e in events if e["reason"] == "role_mismatch"]
    assert len(mismatches) >= 1
    assert mismatches[0]["role"] == "student"
