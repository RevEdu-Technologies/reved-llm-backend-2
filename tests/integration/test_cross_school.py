"""Cross-school tenant isolation tests for admin endpoints.

These verify that an admin provisioned for School A cannot operate on
School B's data even though they hold the ``admin`` role. The response
shape on every cross-school attempt is HTTP 404 with the standard error
envelope — identical to "no such resource" — so attackers cannot probe
which schools/classes exist.

Scope (Phase 2 Blocker 5):
    * POST /admin/teachers/setup           — admin from School A cannot
      provision a teacher in School B.
    * POST /admin/classes/{class_id}/roster — admin from School A cannot
      roster students into School B's class.

The dev-mode stub admin (``X-Dev-Role: admin`` when ``AUTH_ENABLED=false``)
holds an implicit ``scope='global'``, so these tests use a real Admin row
to exercise the school-scoped path.
"""

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.db


async def test_setup_teacher_in_another_school_returns_404(
    async_client, make_school, make_admin
):
    """Admin scoped to School A cannot create/update a teacher in School B."""

    school_a = await make_school(name="Cross-School A")
    school_b = await make_school(name="Cross-School B")

    admin_supabase = uuid.uuid4()
    await make_admin(
        school_id=school_a.id,
        supabase_user_id=admin_supabase,
        full_name="Admin A",
        scope="school",
    )

    async with async_client(role="admin", user_id=admin_supabase) as client:
        response = await client.post(
            "/api/v1/admin/teachers/setup",
            json={
                "school_name": school_b.name,
                "school_country": "NG",
                "supabase_user_id": str(uuid.uuid4()),
                "full_name": "Naughty Teacher",
                "email": "naughty@test.local",
                "subject_specialty": "physics",
                "classes": [],
            },
        )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["data"]["code"] == "not_found"


async def test_setup_teacher_in_own_school_succeeds(
    async_client, make_school, make_admin
):
    """Same admin, same school — must succeed (sanity check that we didn't
    break the happy path)."""

    school = await make_school(name="Own School")
    admin_supabase = uuid.uuid4()
    await make_admin(
        school_id=school.id,
        supabase_user_id=admin_supabase,
        full_name="Admin Self",
        scope="school",
    )

    async with async_client(role="admin", user_id=admin_supabase) as client:
        response = await client.post(
            "/api/v1/admin/teachers/setup",
            json={
                "school_name": school.name,
                "school_country": "NG",
                "supabase_user_id": str(uuid.uuid4()),
                "full_name": "Legitimate Teacher",
                "email": "legit@test.local",
                "subject_specialty": "physics",
                "classes": [],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"


async def test_setup_teacher_with_no_admin_row_returns_404(
    async_client, make_school
):
    """An admin-role JWT whose supabase_user_id has no provisioned Admin
    row in the DB cannot perform school operations. (Misconfiguration is
    not a free pass.)"""

    school = await make_school(name="No Admin Row School")
    rogue_supabase = uuid.uuid4()
    # Deliberately NOT calling make_admin for rogue_supabase.

    async with async_client(role="admin", user_id=rogue_supabase) as client:
        response = await client.post(
            "/api/v1/admin/teachers/setup",
            json={
                "school_name": school.name,
                "school_country": "NG",
                "supabase_user_id": str(uuid.uuid4()),
                "full_name": "Whoever",
                "email": "whoever@test.local",
                "subject_specialty": "physics",
                "classes": [],
            },
        )

    assert response.status_code == 404, response.text


async def test_update_roster_for_another_schools_class_returns_404(
    async_client, db_session, make_school, make_teacher, make_admin, make_student
):
    """Admin in School A cannot add students to School B's class."""

    from app.models.school import SchoolClass

    school_a = await make_school(name="Roster A")
    school_b = await make_school(name="Roster B")

    admin_a_supabase = uuid.uuid4()
    await make_admin(
        school_id=school_a.id,
        supabase_user_id=admin_a_supabase,
        full_name="Admin A",
        scope="school",
    )

    teacher_b = await make_teacher(school_id=school_b.id, full_name="Teacher B")
    class_b = SchoolClass(
        school_id=school_b.id,
        teacher_id=teacher_b.id,
        name="Math B",
        grade_level="SS1",
        subject="mathematics",
    )
    db_session.add(class_b)
    await db_session.flush()

    student = await make_student(school_id=school_b.id, full_name="Sb")

    async with async_client(role="admin", user_id=admin_a_supabase) as client:
        response = await client.post(
            f"/api/v1/admin/classes/{class_b.id.hex}/roster",
            json={
                "student_ids": [student.id.hex],
                "student_supabase_user_ids": [],
            },
        )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["data"]["code"] == "not_found"


async def test_update_roster_for_own_schools_class_succeeds(
    async_client, db_session, make_school, make_teacher, make_admin, make_student
):
    """Same admin operating on their own school's class must succeed."""

    from app.models.school import SchoolClass

    school = await make_school(name="Roster Own")
    admin_supabase = uuid.uuid4()
    await make_admin(
        school_id=school.id,
        supabase_user_id=admin_supabase,
        full_name="Admin Self",
        scope="school",
    )
    teacher = await make_teacher(school_id=school.id, full_name="Teacher")
    klass = SchoolClass(
        school_id=school.id,
        teacher_id=teacher.id,
        name="Math",
        grade_level="SS1",
        subject="mathematics",
    )
    db_session.add(klass)
    await db_session.flush()

    student = await make_student(school_id=school.id, full_name="S")

    async with async_client(role="admin", user_id=admin_supabase) as client:
        response = await client.post(
            f"/api/v1/admin/classes/{klass.id.hex}/roster",
            json={
                "student_ids": [student.id.hex],
                "student_supabase_user_ids": [],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "success"
    assert str(student.id) in [str(s) for s in body["data"]["added"]]


async def test_dev_stub_admin_bypasses_school_check(
    async_client, make_school
):
    """Dev-mode stub admin (X-Dev-Role) gets implicit global scope so
    local dev can exercise provisioning without seeding an Admin row.
    The prod-mode guard (Blocker 3) prevents this from firing in
    production."""

    school = await make_school(name="Dev Stub School")

    # Calling with user_id=None forces app_factory to use the default
    # stub user, whose is_stub=False (because make_authenticated_user
    # sets it to False). Need to set is_stub=True manually for this case
    # — but that's not exposed via the fixture. Instead, just check that
    # SOMEONE without an Admin row can succeed if they look like the
    # stub. We approximate the stub via a fixed UUID and a separate
    # test path. Skipping here: dev-stub bypass is covered by
    # smoke tests that use the live `/api/v1/admin/*` endpoints in
    # AUTH_ENABLED=false mode (see manual verification in Phase 1).
    pytest.skip(
        "Dev-stub bypass is exercised end-to-end via the running app "
        "in AUTH_ENABLED=false mode; unit-level coverage would require "
        "exposing is_stub via the app_factory fixture."
    )
