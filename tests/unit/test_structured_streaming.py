"""Unit tests for the teacher + parent SSE streaming endpoints.

These endpoints return structured JSON, so streaming is split into:
  - ``meta``  — shell-UI context (topic, subject, etc.)
  - ``chunk`` — raw LLM deltas (JSON tokens, may be ignored by the UI)
  - ``done``  — terminal event with the parsed structured payload

Tests stub the service layer via ``app.dependency_overrides``, same
pattern as ``tests/unit/test_student_streaming.py``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import (
    get_lesson_plan_service,
    get_parent_explain_service,
)
from app.schemas.parent import ExplainTopicResponse
from app.schemas.teacher import LessonNotesResponse, LessonSection
from app.services.parent.communication_service import (
    ExplainTopicStreamChunk,
    ExplainTopicStreamDone,
    ExplainTopicStreamEvent,
    ExplainTopicStreamMeta,
)
from app.services.teacher.lesson_plan_service import (
    LessonNotesStreamChunk,
    LessonNotesStreamDone,
    LessonNotesStreamEvent,
    LessonNotesStreamMeta,
)


# --- SSE parsing (same shape as test_student_streaming) ------------------


_EVENT_RE = re.compile(
    r"event:\s*(?P<event>[^\n]+)\ndata:\s*(?P<data>[^\n]*)\n\n",
    re.MULTILINE,
)


def _parse_sse(blob: str) -> list[tuple[str, dict]]:
    return [
        (m["event"].strip(), json.loads(m["data"]))
        for m in _EVENT_RE.finditer(blob)
    ]


async def _read_stream(client: AsyncClient, path: str, body: dict, role: str) -> str:
    async with client.stream(
        "POST", path, json=body, headers={"X-Dev-Role": role}
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        out = ""
        async for piece in resp.aiter_text():
            out += piece
        return out


# --- Teacher lesson-notes ------------------------------------------------


class _StubLessonNotesService:
    def __init__(self, events: list[LessonNotesStreamEvent]) -> None:
        self._events = events

    async def generate_stream(
        self, request, *, user_id=None
    ) -> AsyncIterator[LessonNotesStreamEvent]:
        for ev in self._events:
            yield ev


def _lesson_notes_result(topic: str = "Newton's laws") -> LessonNotesResponse:
    return LessonNotesResponse(
        topic=topic,
        subject="physics",
        student_class="SS2",
        learning_objectives=["Understand inertia."],
        overview="An overview.",
        sections=[
            LessonSection(
                heading="Introduction",
                body="Body.",
                examples=["Example 1"],
            )
        ],
        teacher_tips=["A tip."],
        misconceptions_to_address=["A misconception."],
        sources=["physics.txt"],
        generation_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )


async def test_lesson_notes_stream_happy_path():
    from main import app

    result = _lesson_notes_result()
    stub = _StubLessonNotesService(
        [
            LessonNotesStreamMeta(
                topic=result.topic,
                subject=result.subject,
                student_class=result.student_class,
                conversation_id=result.conversation_id,
            ),
            LessonNotesStreamChunk(text='{"topic": "'),
            LessonNotesStreamChunk(text='Newton\'s laws"}'),
            LessonNotesStreamDone(result=result),
        ]
    )
    app.dependency_overrides[get_lesson_plan_service] = lambda: stub
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            blob = await _read_stream(
                c,
                "/api/v1/teacher/lesson-notes/stream",
                {
                    "subject": "physics",
                    "student_class": "SS2",
                    "topic": "Newton's laws",
                },
                role="teacher",
            )
    finally:
        app.dependency_overrides.pop(get_lesson_plan_service, None)

    events = _parse_sse(blob)
    assert [e for e, _ in events] == ["meta", "chunk", "chunk", "done"]

    meta = events[0][1]
    assert meta["topic"] == "Newton's laws"
    assert meta["subject"] == "physics"
    assert meta["student_class"] == "SS2"
    assert meta["conversation_id"] is not None
    uuid.UUID(meta["conversation_id"])

    done = events[-1][1]
    assert done["result"]["topic"] == "Newton's laws"
    assert done["result"]["subject"] == "physics"
    # The structured payload is the same as non-streaming would return.
    assert len(done["result"]["sections"]) == 1
    assert done["result"]["sections"][0]["heading"] == "Introduction"
    # generation_id rides in the done event so the frontend doesn't
    # need a follow-up round trip.
    assert done["result"]["generation_id"] is not None


async def test_lesson_notes_stream_role_gate_rejects_student():
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.post(
            "/api/v1/teacher/lesson-notes/stream",
            json={
                "subject": "physics",
                "student_class": "SS2",
                "topic": "Q?",
            },
            headers={"X-Dev-Role": "student"},
        )
    assert resp.status_code == 403


async def test_lesson_notes_stream_emits_error_event_on_service_failure():
    """A backend exception should land as a terminal ``error`` SSE event."""

    from main import app

    class _Boom:
        async def generate_stream(self, request, *, user_id=None):
            yield LessonNotesStreamMeta(
                topic=request.topic,
                subject=request.subject,
                student_class=request.student_class,
                conversation_id=uuid.uuid4(),
            )
            raise RuntimeError("boom")

    app.dependency_overrides[get_lesson_plan_service] = lambda: _Boom()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            blob = await _read_stream(
                c,
                "/api/v1/teacher/lesson-notes/stream",
                {
                    "subject": "physics",
                    "student_class": "SS2",
                    "topic": "Q?",
                },
                role="teacher",
            )
    finally:
        app.dependency_overrides.pop(get_lesson_plan_service, None)

    events = _parse_sse(blob)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "stream_failed"
    # We never leak the exception detail to the wire.
    assert "boom" not in json.dumps(events[-1][1])


# --- Parent explain-topic ------------------------------------------------


class _StubExplainService:
    def __init__(self, events: list[ExplainTopicStreamEvent]) -> None:
        self._events = events

    async def explain_stream(
        self, request, *, user_id=None
    ) -> AsyncIterator[ExplainTopicStreamEvent]:
        for ev in self._events:
            yield ev


def _explain_result(topic: str = "Photosynthesis") -> ExplainTopicResponse:
    return ExplainTopicResponse(
        topic=topic,
        subject="biology",
        student_class="JSS2",
        explanation="An explanation.",
        everyday_analogy="An analogy.",
        things_to_try_at_home=["Try this."],
        sources=["bio.txt"],
    )


async def test_explain_topic_stream_happy_path():
    from main import app

    result = _explain_result()
    stub = _StubExplainService(
        [
            ExplainTopicStreamMeta(
                topic=result.topic,
                subject=result.subject,
                student_class=result.student_class,
            ),
            ExplainTopicStreamChunk(text='{"explanation":"'),
            ExplainTopicStreamChunk(text='An explanation."}'),
            ExplainTopicStreamDone(result=result),
        ]
    )
    app.dependency_overrides[get_parent_explain_service] = lambda: stub
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            blob = await _read_stream(
                c,
                "/api/v1/parent/explain-topic/stream",
                {
                    "subject": "biology",
                    "student_class": "JSS2",
                    "topic": "Photosynthesis",
                },
                role="parent",
            )
    finally:
        app.dependency_overrides.pop(get_parent_explain_service, None)

    events = _parse_sse(blob)
    assert [e for e, _ in events] == ["meta", "chunk", "chunk", "done"]
    assert events[0][1]["topic"] == "Photosynthesis"
    assert events[0][1]["subject"] == "biology"
    done = events[-1][1]
    assert done["result"]["topic"] == "Photosynthesis"
    assert done["result"]["explanation"] == "An explanation."
    assert done["result"]["everyday_analogy"] == "An analogy."


async def test_explain_topic_stream_role_gate_rejects_student():
    from main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        resp = await c.post(
            "/api/v1/parent/explain-topic/stream",
            json={
                "subject": "biology",
                "student_class": "JSS2",
                "topic": "Q?",
            },
            headers={"X-Dev-Role": "student"},
        )
    assert resp.status_code == 403
