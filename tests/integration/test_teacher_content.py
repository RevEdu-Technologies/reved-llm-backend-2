"""Integration tests for the frontend-compatible content endpoint.

``POST /teacher/generate-content`` accepts the RevEd web app's existing
payload (numeric grade level, camelCase fields, all five content types)
and streams an OpenAI-style SSE response. The LLM-backed service is
stubbed so these tests assert wiring + contract, not model output.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import pytest

from app.api.dependencies import get_content_service
from app.schemas.teacher import TeacherContentRequest

pytestmark = pytest.mark.asyncio


class _StubContentService:
    """Yields fixed markdown deltas instead of calling Groq."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas
        self.last_request: TeacherContentRequest | None = None

    async def generate_stream(
        self, request: TeacherContentRequest
    ) -> AsyncIterator[str]:
        self.last_request = request
        for delta in self._deltas:
            yield delta


async def test_generate_content_streams_openai_frames(async_client):
    stub = _StubContentService(["# Lesson\n", "Photosynthesis is..."])
    from main import app as fastapi_app

    fastapi_app.dependency_overrides[get_content_service] = lambda: stub
    try:
        async with async_client(role="teacher", user_id=uuid.uuid4()) as client:
            resp = await client.post(
                "/api/v1/teacher/generate-content",
                json={
                    "contentType": "slides",
                    "subject": "Science",  # umbrella subject -> must not 422
                    "gradeLevel": 9,  # numeric -> JSS3
                    "topic": "Photosynthesis",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text
        # OpenAI-style frames: data: {"choices":[{"delta":{"content":...}}]}
        assert '"delta":{"content":"# Lesson\\n"}' in body
        assert "Photosynthesis is..." in body
        assert body.rstrip().endswith("data: [DONE]")
        # The numeric grade + umbrella subject were normalized for the service.
        assert stub.last_request is not None
        assert stub.last_request.student_class == "JSS3"
        assert stub.last_request.subject == "general"
    finally:
        fastapi_app.dependency_overrides.pop(get_content_service, None)


async def test_generate_content_rejects_unmappable_grade(async_client):
    async with async_client(role="teacher", user_id=uuid.uuid4()) as client:
        resp = await client.post(
            "/api/v1/teacher/generate-content",
            json={
                "contentType": "notes",
                "subject": "biology",
                "gradeLevel": "not-a-grade",
                "topic": "Cells",
            },
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["data"]["code"] == "validation_error"


async def test_generate_content_requires_teacher_role(async_client):
    async with async_client(role="student", user_id=uuid.uuid4()) as client:
        resp = await client.post(
            "/api/v1/teacher/generate-content",
            json={
                "contentType": "lesson_plan",
                "subject": "biology",
                "gradeLevel": 10,
                "topic": "Cells",
            },
        )
    assert resp.status_code == 403, resp.text
