"""Smoke tests for the shared fixtures in ``tests/conftest.py``.

These don't test product behavior — they prove the test infrastructure
itself works (DB connection, transactional rollback, factories, app
dependency overrides, async client). If any of these fail, every other
DB-backed test will fail too, so they're the first thing to look at when
the suite breaks.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.school import School
from app.models.student import Student
from app.models.teacher import Teacher


pytestmark = pytest.mark.db


async def test_lightweight_fixtures_compose(make_jwt, auth_headers, make_authenticated_user):
    token = make_jwt(role="teacher")
    assert token.count(".") == 2  # JWT has three segments

    headers = auth_headers(role="parent")
    assert headers == {"X-Dev-Role": "parent"}

    bearer = auth_headers(role="admin", mode="bearer")
    assert bearer["Authorization"].startswith("Bearer ")

    user = make_authenticated_user(role="teacher")
    assert user.role == "teacher"
    assert user.is_stub is False


async def test_db_session_persists_within_test(db_session, make_school):
    school = await make_school(name="Smoke School")
    fetched = await db_session.get(School, school.id)
    assert fetched is not None
    assert fetched.name == "Smoke School"


async def test_db_session_is_rolled_back_between_tests(db_session):
    """The school created above must NOT be visible here."""

    result = await db_session.execute(
        select(School).where(School.name == "Smoke School")
    )
    assert result.scalar_one_or_none() is None


async def test_factories_link_correctly(
    db_session, make_school, make_teacher, make_parent, make_student
):
    school = await make_school(name="Linkage School")
    teacher = await make_teacher(school_id=school.id, full_name="T")
    parent = await make_parent(full_name="P")
    student = await make_student(
        school_id=school.id, parent_id=parent.id, full_name="S"
    )

    # Round-trip via DB to confirm everything actually persisted to the
    # rolled-back transaction.
    db_session.expunge_all()
    fetched_teacher = (
        await db_session.execute(select(Teacher).where(Teacher.id == teacher.id))
    ).scalar_one()
    fetched_student = (
        await db_session.execute(select(Student).where(Student.id == student.id))
    ).scalar_one()
    assert fetched_teacher.school_id == school.id
    assert fetched_student.school_id == school.id
    assert fetched_student.parent_id == parent.id


async def test_two_schools_composite_fixture(two_schools):
    assert two_schools["school_a"].id != two_schools["school_b"].id
    assert two_schools["teacher_a"].school_id == two_schools["school_a"].id
    assert two_schools["teacher_b"].school_id == two_schools["school_b"].id
    assert two_schools["student_a"].parent_id == two_schools["parent_a"].id


async def test_async_client_health_endpoint(async_client):
    async with async_client(role="student") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"status", "data", "message", "role"}
    assert body["status"] == "success"
    assert body["data"] == {"status": "ok"}


async def test_async_client_honors_role_override(
    async_client, make_school, make_teacher
):
    """The app_factory's user override must take precedence over X-Dev-Role.

    We send an X-Dev-Role of 'student' but configure the override to
    'parent' — the parent-only endpoint should accept us, proving the
    dependency override won.
    """

    teacher_user_id = uuid.uuid4()
    async with async_client(role="parent", user_id=teacher_user_id) as client:
        # Hitting the parent endpoint — would 403 if the X-Dev-Role won.
        response = await client.get(
            "/api/v1/parent/child-activity",
            headers={"X-Dev-Role": "student"},  # decoy
        )
    # Even if the DB returns "no children" the call must be authorized.
    assert response.status_code in {200, 404}, response.text
    body = response.json()
    assert body["role"] == "parent"
