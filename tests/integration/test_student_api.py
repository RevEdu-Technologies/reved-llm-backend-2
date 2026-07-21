"""Integration tests for the student API endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.rag.query_engine.engine import GroundedQAEngine
from app.rag.query_engine.router import RoleAwareQueryRouter
from app.rag.retrieval.retriever import RetrievalResult
from app.services.student.tutor_service import StudentTutorService
from main import app


class _FakeRetriever:
    def __init__(self, results):
        self._results = results

    def retrieve(self, query_text, *, top_k=5, subject=None, namespace=None):
        return self._results


class _FakeLLMClient:
    def __init__(self, answer: str):
        self._answer = answer

    def generate(self, *, system_prompt, user_prompt):
        return type("R", (), {"text": self._answer})


def _stub_tutor_service(
    answer: str = "Photosynthesis is how plants make food from sunlight.",
) -> StudentTutorService:
    results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="bio.txt",
            subject="biology",
            content_type="text",
            chunk_index=0,
            text="Plants convert sunlight to food.",
        )
    ]
    engine = GroundedQAEngine(
        retriever=_FakeRetriever(results),
        llm_client=_FakeLLMClient(answer),
    )
    return StudentTutorService(router=RoleAwareQueryRouter(engine=engine))


@pytest.fixture()
def client():
    from app.api.dependencies import get_tutor_service

    service = _stub_tutor_service()
    app.dependency_overrides[get_tutor_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def _envelope_payload(resp):
    body = resp.json()
    assert set(body.keys()) == {"status", "data", "message", "role"}
    return body


class TestAskEndpoint:
    """POST /api/v1/student/ask"""

    def test_successful_request(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is photosynthesis?",
                "student_class": "Primary 5",
                "subject": "biology",
            },
        )
        assert resp.status_code == 200
        body = _envelope_payload(resp)
        assert body["status"] == "success"
        assert body["role"] == "student"
        data = body["data"]
        assert data["student_class"] == "Primary 5"
        assert data["subject"] == "biology"
        assert "Photosynthesis" in data["answer"]

    def test_successful_request_without_subject(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is energy?",
                "student_class": "JSS2",
            },
        )
        assert resp.status_code == 200
        body = _envelope_payload(resp)
        assert body["data"]["subject"] is None

    def test_response_does_not_contain_internal_fields(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is photosynthesis?",
                "student_class": "SS1",
            },
        )
        assert resp.status_code == 200
        data = _envelope_payload(resp)["data"]
        # ``conversation_id`` is part of the public schema as of the chat
        # persistence work — it's a thread identifier the frontend round-trips,
        # not an internal field. The negative assertions below remain the
        # real intent of the test (no RAG / retrieval internals leak).
        assert set(data.keys()) == {
            "status",
            "answer",
            "student_class",
            "subject",
            "original_question",
            "corrected_question",
            "original_subject",
            "clarifying_question",
            "conversation_id",
        }
        assert "sources" not in data
        assert "retrieved_chunks" not in data
        assert "score" not in data

    def test_empty_question_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "",
                "student_class": "JSS1",
            },
        )
        assert resp.status_code == 422
        body = _envelope_payload(resp)
        assert body["status"] == "error"
        assert body["role"] == "student"

    def test_invalid_student_class_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is photosynthesis?",
                "student_class": "Grade 10",
            },
        )
        assert resp.status_code == 422

    def test_non_canonical_subject_is_accepted_and_normalized_downstream(
        self, client: TestClient
    ):
        """Subjects like 'math' or typos no longer 422 — the preflight layer
        handles normalization or raises a clarifier."""

        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is photosynthesis?",
                "student_class": "JSS1",
                "subject": "math",
            },
        )
        assert resp.status_code == 200

    def test_missing_student_class_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/student/ask",
            json={
                "question": "What is energy?",
            },
        )
        assert resp.status_code == 422


class TestHealthEndpoint:
    """GET /api/v1/health"""

    def test_health_returns_ok(self, client: TestClient):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = _envelope_payload(resp)
        assert body["status"] == "success"
        assert body["role"] == "system"
        assert body["data"] == {"status": "ok"}
