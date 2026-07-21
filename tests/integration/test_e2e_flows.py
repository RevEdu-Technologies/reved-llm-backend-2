"""End-to-end flow tests for the four user roles.

Each test exercises a realistic multi-call journey for one role against
the real Postgres test DB. LLM-bound services (tutor, lesson-notes,
quiz, feedback, explain-topic) are stubbed via FastAPI dependency
overrides so the tests are deterministic and fast — the goal is to
prove that auth, validation, persistence, and the response envelope
work all the way through, not to re-test the LLM glue (that's covered
by ``tests/unit/test_*``).

Flows
-----
* ``test_student_full_flow``  — signup-equivalent (factory), ask,
  list conversations, create goal, update goal progress, list goals.
* ``test_teacher_full_flow``  — signup-equivalent, lesson-notes, quiz,
  list generations, class-progress.
* ``test_parent_full_flow``   — signup-equivalent (parent + linked
  child), explain-topic, child-activity, list parent generations.
* ``test_admin_full_flow``    — global-scope admin: provision teacher,
  provision parent, roster a class, usage-summary, send notification.

Why one test per role instead of many small ones
------------------------------------------------
Phase 2 captured a test-infra gotcha: chaining two API round-trips
inside ONE ``async with async_client(...)`` block can close the test
session's savepoint mid-flight when the first call commits. The
workaround is one round-trip per ``async with`` block. Bundling each
flow into one test lets each step open its own client cleanly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Lightweight service stubs
# ---------------------------------------------------------------------------


class _StubTutorService:
    """Stand-in for ``StudentTutorService`` used by the student flow."""

    def __init__(self) -> None:
        self._conversations: dict[uuid.UUID, list[dict]] = {}
        self._user_threads: dict[uuid.UUID, list[uuid.UUID]] = {}

    async def ask(
        self,
        *,
        question: str,
        student_class: str,
        subject: str | None,
        history,
        learning_state,
        user_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
    ):
        cid = conversation_id or uuid.uuid4()
        if user_id is not None:
            self._user_threads.setdefault(user_id, []).append(cid)
            self._conversations.setdefault(cid, []).extend(
                [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": f"Answer to: {question}"},
                ]
            )
        return type(
            "TutorResult",
            (),
            {
                "status": "answered",
                "answer": f"Answer to: {question}",
                "student_class": student_class,
                "subject": subject,
                "original_question": None,
                "corrected_question": None,
                "original_subject": None,
                "clarifying_question": None,
                "conversation_id": cid,
            },
        )

    async def list_conversations(self, *, user_id: uuid.UUID | None):
        if user_id is None:
            return []
        out = []
        for cid in self._user_threads.get(user_id, []):
            turns = self._conversations.get(cid, [])
            if not turns:
                continue
            now = datetime.now(timezone.utc)
            out.append(
                {
                    "conversation_id": cid,
                    "subject": "physics",
                    "message_count": len(turns),
                    "last_question_preview": turns[0]["content"][:120],
                    "started_at": now - timedelta(minutes=5),
                    "last_active_at": now,
                }
            )
        return out

    async def conversation_history(
        self, *, conversation_id: uuid.UUID, user_id: uuid.UUID | None
    ):
        if user_id is None:
            return []
        if conversation_id not in self._user_threads.get(user_id, []):
            return []
        from app.schemas.student import ConversationTurn

        return [
            ConversationTurn(role=t["role"], content=t["content"])
            for t in self._conversations.get(conversation_id, [])
        ]


class _StubLessonNotesService:
    async def generate(self, request, *, user_id=None):
        from app.schemas.teacher import LessonNotesResponse, LessonSection

        return LessonNotesResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            learning_objectives=["Understand the basics."],
            overview="An overview of the topic.",
            sections=[
                LessonSection(
                    heading="Introduction",
                    body="Body text for the introduction.",
                    examples=["Example 1"],
                )
            ],
            teacher_tips=["Watch for misconceptions."],
            misconceptions_to_address=["Common error A"],
            sources=["test-source.txt"],
            generation_id=uuid.uuid4(),
            conversation_id=request.conversation_id or uuid.uuid4(),
        )


class _StubQuizService:
    async def generate(self, request, *, user_id=None):
        from app.schemas.teacher import QuizQuestion, QuizResponse

        return QuizResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            questions=[
                QuizQuestion(
                    question_number=i + 1,
                    question=f"Q{i+1}: explain the topic.",
                    question_type="short_answer",
                    difficulty="medium",
                    marking_guide="Look for clarity.",
                    points=1,
                )
                for i in range(request.num_questions)
            ],
            total_points=request.num_questions,
            suggested_duration_minutes=30,
            sources=["test-source.txt"],
            generation_id=uuid.uuid4(),
            conversation_id=request.conversation_id or uuid.uuid4(),
        )


class _StubExplainService:
    async def explain(self, request, *, user_id=None):
        from app.schemas.parent import ExplainTopicResponse

        return ExplainTopicResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            explanation="A plain-language explanation for the parent.",
            everyday_analogy="It's like ... an analogy.",
            things_to_try_at_home=["Try this together."],
            sources=["test-source.txt"],
        )


def _override_service(app, dep_factory, stub):
    """Pin a dependency override to ``stub`` for the duration of one flow."""

    app.dependency_overrides[dep_factory] = lambda: stub


# ---------------------------------------------------------------------------
# Student flow
# ---------------------------------------------------------------------------


async def test_student_full_flow(async_client, app_factory, make_school, make_student):
    """Student: sign up (factory) → ask → list conversations → create
    goal → update goal progress → list goals."""

    from app.api.dependencies import get_tutor_service

    school = await make_school(name="E2E School Student")
    me_supabase = uuid.uuid4()
    me = await make_student(
        school_id=school.id, supabase_user_id=me_supabase, full_name="E2E Student"
    )

    tutor_stub = _StubTutorService()

    # Each round-trip opens its own client to keep the test session's
    # savepoint state coherent across commits.

    # Step 1 — POST /student/ask
    app = app_factory(role="student", user_id=me_supabase)
    _override_service(app, get_tutor_service, tutor_stub)
    async with async_client(role="student", user_id=me_supabase) as client:
        resp = await client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is photosynthesis?",
                "student_class": "JSS2",
                "subject": "biology",
            },
        )
    assert resp.status_code == 200, resp.text
    ask_body = resp.json()
    assert ask_body["status"] == "success"
    assert ask_body["role"] == "student"
    assert "Answer to:" in ask_body["data"]["answer"]
    conversation_id = ask_body["data"]["conversation_id"]
    assert conversation_id is not None

    # Step 2 — GET /student/conversations
    app = app_factory(role="student", user_id=me_supabase)
    _override_service(app, get_tutor_service, tutor_stub)
    async with async_client(role="student", user_id=me_supabase) as client:
        resp = await client.get("/api/v1/student/conversations")
    assert resp.status_code == 200, resp.text
    convs = resp.json()["data"]["conversations"]
    assert any(c["conversation_id"] == conversation_id for c in convs)

    # Step 3 — POST /student/goals
    async with async_client(role="student", user_id=me_supabase) as client:
        resp = await client.post(
            "/api/v1/student/goals",
            json={
                "student_id": me.id.hex,
                "title": "Master cell biology",
                "description": "Cover Cells, Tissues, Organs.",
                "subject": "biology",
                "target_date": None,
            },
        )
    assert resp.status_code == 200, resp.text
    goal_body = resp.json()
    assert goal_body["status"] == "success"
    goal_id = goal_body["data"]["id"]
    assert goal_body["data"]["progress_percent"] == 0

    # Step 4 — PATCH /student/goals/{goal_id}/progress
    async with async_client(role="student", user_id=me_supabase) as client:
        resp = await client.patch(
            f"/api/v1/student/goals/{goal_id}/progress",
            json={"progress_percent": 50, "note": "Halfway there"},
        )
    assert resp.status_code == 200, resp.text
    upd_body = resp.json()
    assert upd_body["data"]["progress_percent"] == 50

    # Step 5 — GET /student/goals/{student_id}
    async with async_client(role="student", user_id=me_supabase) as client:
        resp = await client.get(f"/api/v1/student/goals/{me.id.hex}")
    assert resp.status_code == 200, resp.text
    list_body = resp.json()
    goals = list_body["data"]["goals"]
    assert any(g["id"] == goal_id and g["progress_percent"] == 50 for g in goals)


# ---------------------------------------------------------------------------
# Teacher flow
# ---------------------------------------------------------------------------


async def test_teacher_full_flow(
    async_client,
    app_factory,
    db_session,
    make_school,
    make_teacher,
    make_student,
    make_membership,
):
    """Teacher: signup (factory) + class with one rostered student →
    lesson notes → quiz → list generations → class-progress."""

    from app.api.dependencies import (
        get_lesson_plan_service,
        get_quiz_service,
    )
    from app.models.school import SchoolClass

    school = await make_school(name="E2E School Teacher")
    teacher_supabase = uuid.uuid4()
    teacher = await make_teacher(
        school_id=school.id,
        supabase_user_id=teacher_supabase,
        full_name="E2E Teacher",
        subject_specialty="physics",
    )
    klass = SchoolClass(
        school_id=school.id,
        teacher_id=teacher.id,
        name="SS2 Physics",
        grade_level="SS2",
        subject="physics",
    )
    db_session.add(klass)
    await db_session.flush()
    student = await make_student(school_id=school.id, full_name="Pupil A")
    await make_membership(student_id=student.id, class_id=klass.id)

    lesson_stub = _StubLessonNotesService()
    quiz_stub = _StubQuizService()

    # Step 1 — POST /teacher/lesson-notes
    app = app_factory(role="teacher", user_id=teacher_supabase)
    _override_service(app, get_lesson_plan_service, lesson_stub)
    async with async_client(role="teacher", user_id=teacher_supabase) as client:
        resp = await client.post(
            "/api/v1/teacher/lesson-notes",
            json={
                "subject": "physics",
                "student_class": "SS2",
                "topic": "Newton's laws of motion",
                "include_examples": True,
            },
        )
    assert resp.status_code == 200, resp.text
    lesson_body = resp.json()
    assert lesson_body["status"] == "success"
    assert lesson_body["data"]["topic"] == "Newton's laws of motion"
    assert len(lesson_body["data"]["sections"]) >= 1

    # Step 2 — POST /teacher/quiz
    app = app_factory(role="teacher", user_id=teacher_supabase)
    _override_service(app, get_quiz_service, quiz_stub)
    async with async_client(role="teacher", user_id=teacher_supabase) as client:
        resp = await client.post(
            "/api/v1/teacher/quiz",
            json={
                "subject": "physics",
                "student_class": "SS2",
                "topic": "Kinematics",
                "num_questions": 3,
            },
        )
    assert resp.status_code == 200, resp.text
    quiz_body = resp.json()
    assert quiz_body["status"] == "success"
    assert len(quiz_body["data"]["questions"]) == 3

    # Step 3 — GET /teacher/generations
    # Note: stub services don't persist, so we just assert the endpoint
    # responds with the right envelope shape and a list (possibly empty).
    async with async_client(role="teacher", user_id=teacher_supabase) as client:
        resp = await client.get("/api/v1/teacher/generations")
    assert resp.status_code == 200, resp.text
    gen_body = resp.json()
    assert gen_body["status"] == "success"
    assert "generations" in gen_body["data"]

    # Step 4 — GET /teacher/class-progress
    async with async_client(role="teacher", user_id=teacher_supabase) as client:
        resp = await client.get("/api/v1/teacher/class-progress")
    assert resp.status_code == 200, resp.text
    prog_body = resp.json()
    assert prog_body["status"] == "success"
    assert "total_student_questions" in prog_body["data"]
    assert prog_body["data"]["scope"] in {"teacher_classes", "global_fallback"}


# ---------------------------------------------------------------------------
# Parent flow
# ---------------------------------------------------------------------------


async def test_parent_full_flow(
    async_client, app_factory, make_school, make_parent, make_student
):
    """Parent: signup (factory) with linked child → explain-topic →
    child-activity → list parent generations."""

    from app.api.dependencies import get_parent_explain_service

    school = await make_school(name="E2E School Parent")
    parent_supabase = uuid.uuid4()
    parent = await make_parent(
        supabase_user_id=parent_supabase, full_name="E2E Parent"
    )
    await make_student(
        school_id=school.id, parent_id=parent.id, full_name="Child A"
    )

    explain_stub = _StubExplainService()

    # Step 1 — POST /parent/explain-topic
    app = app_factory(role="parent", user_id=parent_supabase)
    _override_service(app, get_parent_explain_service, explain_stub)
    async with async_client(role="parent", user_id=parent_supabase) as client:
        resp = await client.post(
            "/api/v1/parent/explain-topic",
            json={
                "subject": "biology",
                "student_class": "JSS2",
                "topic": "Photosynthesis",
                "child_question": "Why are leaves green?",
            },
        )
    assert resp.status_code == 200, resp.text
    exp_body = resp.json()
    assert exp_body["status"] == "success"
    assert exp_body["data"]["topic"] == "Photosynthesis"
    assert "everyday_analogy" in exp_body["data"]

    # Step 2 — GET /parent/child-activity
    async with async_client(role="parent", user_id=parent_supabase) as client:
        resp = await client.get("/api/v1/parent/child-activity")
    assert resp.status_code == 200, resp.text
    activity_body = resp.json()
    assert activity_body["status"] == "success"
    assert activity_body["data"]["parent_user_id"] == str(parent_supabase)
    # We added one child to this parent — they should appear (with zero
    # activity, since no /student/ask traffic exists in this test).
    assert len(activity_body["data"]["children"]) == 1
    assert activity_body["data"]["children"][0]["student_name"] == "Child A"

    # Step 3 — GET /parent/generations
    async with async_client(role="parent", user_id=parent_supabase) as client:
        resp = await client.get("/api/v1/parent/generations")
    assert resp.status_code == 200, resp.text
    plist_body = resp.json()
    assert plist_body["status"] == "success"
    assert "generations" in plist_body["data"]


# ---------------------------------------------------------------------------
# Admin flow
# ---------------------------------------------------------------------------


async def test_admin_full_flow(
    async_client, db_session, make_school, make_admin, make_student
):
    """Admin (global scope): provision teacher → provision parent →
    roster a class → usage-summary → send notification."""

    school = await make_school(name="E2E School Admin")
    admin_supabase = uuid.uuid4()
    await make_admin(
        school_id=school.id,
        supabase_user_id=admin_supabase,
        full_name="E2E Admin",
        scope="global",
    )

    # Step 1 — POST /admin/teachers/setup (also creates one class)
    teacher_supabase = uuid.uuid4()
    async with async_client(role="admin", user_id=admin_supabase) as client:
        resp = await client.post(
            "/api/v1/admin/teachers/setup",
            json={
                "school_name": school.name,
                "school_country": "NG",
                "supabase_user_id": str(teacher_supabase),
                "full_name": "Provisioned Teacher",
                "email": "pt@test.local",
                "subject_specialty": "physics",
                "classes": [
                    {"name": "SS1 Physics", "subject": "physics", "grade_level": "SS1"}
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    teach_body = resp.json()
    assert teach_body["status"] == "success"
    class_ids = teach_body["data"]["class_ids"]
    assert len(class_ids) == 1
    class_id = class_ids[0]

    # Step 2 — POST /admin/parents/setup (with one child)
    parent_supabase = uuid.uuid4()
    async with async_client(role="admin", user_id=admin_supabase) as client:
        resp = await client.post(
            "/api/v1/admin/parents/setup",
            json={
                "supabase_user_id": str(parent_supabase),
                "full_name": "Provisioned Parent",
                "email": "pp@test.local",
                "phone": None,
                "children": [
                    {"full_name": "Provisioned Child", "grade_level": "SS1"}
                ],
            },
        )
    assert resp.status_code == 200, resp.text
    par_body = resp.json()
    assert par_body["status"] == "success"
    assert len(par_body["data"]["student_ids"]) == 1

    # Step 3 — POST /admin/classes/{class_id}/roster
    # Pre-seed a student in the same school to enrol.
    enrol_me = await make_student(school_id=school.id, full_name="To Enrol")
    async with async_client(role="admin", user_id=admin_supabase) as client:
        resp = await client.post(
            f"/api/v1/admin/classes/{class_id}/roster",
            json={
                "student_ids": [enrol_me.id.hex],
                "student_supabase_user_ids": [],
            },
        )
    assert resp.status_code == 200, resp.text
    roster_body = resp.json()
    assert roster_body["status"] == "success"
    assert enrol_me.id.hex in [s.replace("-", "") for s in roster_body["data"]["added"]]

    # Step 4 — GET /admin/usage-summary
    async with async_client(role="admin", user_id=admin_supabase) as client:
        resp = await client.get("/api/v1/admin/usage-summary")
    assert resp.status_code == 200, resp.text
    usage_body = resp.json()
    assert usage_body["status"] == "success"
    for key in (
        "total_student_questions",
        "total_ai_generations",
        "generations_by_role",
        "schools",
        "teachers",
        "parents",
        "students",
    ):
        assert key in usage_body["data"]

    # Step 5 — POST /admin/notifications (deliver to the provisioned teacher)
    async with async_client(role="admin", user_id=admin_supabase) as client:
        resp = await client.post(
            "/api/v1/admin/notifications",
            json={
                "recipient_user_id": str(teacher_supabase),
                "recipient_role": "teacher",
                "category": "info",
                "title": "Welcome aboard",
                "body": "You've been provisioned. Please log in.",
                "payload": {"link": "/teacher"},
            },
        )
    assert resp.status_code == 200, resp.text
    notif_body = resp.json()
    assert notif_body["status"] == "success"
    assert notif_body["data"]["recipient_user_id"] == str(teacher_supabase)
    assert notif_body["data"]["recipient_role"] == "teacher"
    assert notif_body["data"]["is_read"] is False
