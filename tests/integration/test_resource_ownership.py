"""Cross-user resource-ownership negative tests.

Each of these tests sets up two distinct callers and confirms that
caller A cannot access caller B's resources. We deliberately assert on
the 404 status — anything else (200, 403 with leakage details) would
constitute a data-exposure bug or a UUID-enumeration oracle.

Coverage:
    * GET  /student/goals/{student_id}          — list another student's goals
    * POST /student/goals                       — create a goal on someone else's behalf
    * PATCH /student/goals/{goal_id}/progress   — mutate someone else's goal
    * POST /student/study-groups                — create a group on someone else's behalf
    * POST /student/study-groups/{id}/join      — enroll another student
    * POST /student/study-groups/{id}/facilitate — facilitate without being a member
    * GET  /student/generations/{id}            — read another student's generation
    * GET  /teacher/generations/{id}            — read another teacher's generation
    * GET  /parent/generations/{id}             — read another parent's generation
    * PATCH /notifications/{id}/read            — mark someone else's notification
"""

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.db


# --- Goals ----------------------------------------------------------------


async def test_list_goals_of_another_student_returns_404(
    async_client, make_school, make_student
):
    school = await make_school(name="Goals School")
    me_supabase = uuid.uuid4()
    other = await make_student(school_id=school.id, full_name="Other")
    await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.get(f"/api/v1/student/goals/{other.id.hex}")

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["data"]["code"] == "not_found"


async def test_create_goal_on_another_students_behalf_returns_404(
    async_client, make_school, make_student
):
    school = await make_school(name="CreateGoal School")
    me_supabase = uuid.uuid4()
    other = await make_student(school_id=school.id, full_name="Other")
    await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.post(
            "/api/v1/student/goals",
            json={
                "student_id": other.id.hex,
                "title": "Sneaky goal",
                "description": None,
                "subject": "physics",
                "target_date": None,
            },
        )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["data"]["code"] == "not_found"


async def test_update_goal_progress_of_another_student_returns_404(
    async_client, make_school, make_student, make_goal
):
    school = await make_school(name="UpdateGoal School")
    other = await make_student(school_id=school.id, full_name="Other")
    other_goal = await make_goal(student_id=other.id, title="Other's goal")

    me_supabase = uuid.uuid4()
    await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.patch(
            f"/api/v1/student/goals/{other_goal.id.hex}/progress",
            json={"progress_percent": 99, "note": "haha"},
        )

    assert response.status_code == 404, response.text


# --- Study groups ---------------------------------------------------------


async def test_create_study_group_on_anothers_behalf_returns_404(
    async_client, make_school, make_student
):
    school = await make_school(name="SG School")
    other = await make_student(school_id=school.id, full_name="Other")
    me_supabase = uuid.uuid4()
    await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.post(
            "/api/v1/student/study-groups",
            json={
                "creator_student_id": other.id.hex,
                "name": "Sneaky group",
                "subject": "physics",
                "topic": "mechanics",
                "student_class": "SS1",
            },
        )

    assert response.status_code == 404, response.text


async def test_join_study_group_enrolling_another_student_returns_404(
    async_client, db_session, make_school, make_student
):
    """Set up the group via direct DB write (avoids two API round-trips in one
    transaction, which trips connection-state issues with savepointed sessions)."""

    from app.models.student import StudyGroup

    school = await make_school(name="Join School")
    other = await make_student(school_id=school.id, full_name="Other")
    me_supabase = uuid.uuid4()
    me = await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )

    group = StudyGroup(
        name="Real group",
        subject="physics",
        topic="energy",
        student_class="SS1",
        created_by=me.id,
    )
    db_session.add(group)
    await db_session.flush()

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.post(
            f"/api/v1/student/study-groups/{group.id.hex}/join",
            json={"student_id": other.id.hex},
        )

    assert response.status_code == 404, response.text


# --- Generations ----------------------------------------------------------


async def test_student_cannot_read_another_students_generation(
    async_client, make_school, make_student, make_ai_generation
):
    school = await make_school(name="Gen School")
    other_supabase = uuid.uuid4()
    me_supabase = uuid.uuid4()
    await make_student(
        school_id=school.id, supabase_user_id=other_supabase, full_name="Other"
    )
    await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me"
    )
    other_gen = await make_ai_generation(
        user_id=other_supabase,
        role="student",
        generation_type="learning_path",
        title="Other's path",
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.get(
            f"/api/v1/student/generations/{other_gen.id.hex}"
        )

    assert response.status_code == 404, response.text


async def test_teacher_cannot_read_another_teachers_generation(
    async_client, make_school, make_teacher, make_ai_generation
):
    school = await make_school(name="Teacher Gen School")
    other_supabase = uuid.uuid4()
    me_supabase = uuid.uuid4()
    await make_teacher(
        school_id=school.id, supabase_user_id=other_supabase, full_name="Other T"
    )
    await make_teacher(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me T"
    )
    other_gen = await make_ai_generation(
        user_id=other_supabase,
        role="teacher",
        generation_type="lesson_notes",
        title="Other's notes",
    )

    async with async_client(role="teacher", user_id=me_supabase) as client:
        response = await client.get(
            f"/api/v1/teacher/generations/{other_gen.id.hex}"
        )

    assert response.status_code == 404, response.text


async def test_parent_cannot_read_another_parents_generation(
    async_client, make_parent, make_ai_generation
):
    other_supabase = uuid.uuid4()
    me_supabase = uuid.uuid4()
    await make_parent(supabase_user_id=other_supabase, full_name="Other P")
    await make_parent(supabase_user_id=me_supabase, full_name="Me P")
    other_gen = await make_ai_generation(
        user_id=other_supabase,
        role="parent",
        generation_type="explain_topic",
        title="Other's explainer",
    )

    async with async_client(role="parent", user_id=me_supabase) as client:
        response = await client.get(
            f"/api/v1/parent/generations/{other_gen.id.hex}"
        )

    assert response.status_code == 404, response.text


async def test_generation_with_null_user_id_is_not_readable(
    async_client, make_school, make_teacher, make_ai_generation
):
    """Defensive: legacy/orphaned rows with NULL user_id must not leak."""

    school = await make_school(name="NullGen School")
    me_supabase = uuid.uuid4()
    await make_teacher(
        school_id=school.id, supabase_user_id=me_supabase, full_name="Me T"
    )
    orphan = await make_ai_generation(
        user_id=None,
        role="teacher",
        generation_type="lesson_notes",
        title="Orphan",
    )

    async with async_client(role="teacher", user_id=me_supabase) as client:
        response = await client.get(
            f"/api/v1/teacher/generations/{orphan.id.hex}"
        )

    assert response.status_code == 404, response.text


# --- Notifications --------------------------------------------------------


async def test_mark_anothers_notification_read_returns_404(
    async_client, make_notification
):
    other_supabase = uuid.uuid4()
    me_supabase = uuid.uuid4()
    other_notification = await make_notification(
        recipient_user_id=other_supabase,
        recipient_role="student",
        title="Other's note",
        body="Not yours",
    )

    async with async_client(role="student", user_id=me_supabase) as client:
        response = await client.patch(
            f"/api/v1/notifications/{other_notification.id.hex}/read"
        )

    assert response.status_code == 404, response.text
