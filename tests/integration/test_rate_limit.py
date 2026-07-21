"""Verify that slowapi's per-caller cap fires on LLM endpoints and that
the 429 carries the standard RevEd envelope.

We hammer ``POST /api/v1/student/ask`` more than ``LLM_LIMIT`` times in a
single test using a stubbed tutor service (no Groq / Pinecone / DB I/O
on the hot path) and assert that the first ``LLM_LIMIT`` calls succeed
and the next ones return 429 with ``code='rate_limited'``.

The module-level limiter must be reset before the test because counters
persist across tests in the same process. ``Limiter.reset()`` flushes
the underlying storage (memory:// in CI).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_tutor_service
from app.core.rate_limit import LLM_LIMIT, limiter
from app.rag.query_engine.engine import GroundedQAEngine
from app.rag.query_engine.router import RoleAwareQueryRouter
from app.rag.retrieval.retriever import RetrievalResult
from app.services.student.tutor_service import StudentTutorService
from main import app


class _FakeRetriever:
    def retrieve(self, query_text, *, top_k=5, subject=None, namespace=None):
        return [
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


class _FakeLLM:
    def generate(self, *, system_prompt, user_prompt):
        return type("R", (), {"text": "stubbed answer"})


def _stub_service() -> StudentTutorService:
    engine = GroundedQAEngine(retriever=_FakeRetriever(), llm_client=_FakeLLM())
    return StudentTutorService(router=RoleAwareQueryRouter(engine=engine))


@pytest.fixture
def stubbed_tutor_client():
    app.dependency_overrides[get_tutor_service] = _stub_service
    try:
        limiter.reset()
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        limiter.reset()


def _limit_count() -> int:
    """Parse the integer prefix off ``LLM_LIMIT`` (e.g. '10/minute' → 10)."""

    return int(LLM_LIMIT.split("/", 1)[0])


def test_llm_endpoint_returns_429_with_envelope_after_limit(stubbed_tutor_client):
    cap = _limit_count()
    body = {
        "question": "What is photosynthesis?",
        "student_class": "Primary 5",
        "subject": "biology",
    }

    statuses: list[int] = []
    last_response = None
    # +2 calls past the cap so we are unambiguously over.
    for _ in range(cap + 2):
        resp = stubbed_tutor_client.post("/api/v1/student/ask", json=body)
        statuses.append(resp.status_code)
        last_response = resp

    assert statuses[:cap] == [200] * cap, (
        f"Expected first {cap} calls to succeed, got {statuses}"
    )
    assert statuses[cap] == 429, (
        f"Expected call #{cap + 1} to be rate-limited, got {statuses}"
    )

    envelope = last_response.json()
    assert set(envelope.keys()) == {"status", "data", "message", "role"}
    assert envelope["status"] == "error"
    assert envelope["role"] == "student"
    assert envelope["data"]["code"] == "rate_limited"
    assert "rate limit" in envelope["message"].lower()
    assert last_response.headers.get("retry-after") == "60"
