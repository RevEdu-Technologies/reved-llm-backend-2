"""Tiered LLM rate limits — paid callers get a higher cap than free.

The default ``free`` tier keeps the historical ``10/minute`` LLM cap; a
``premium`` caller (signalled in dev via the ``X-Dev-Tier`` header, in
production via a verified JWT ``subscription_tier`` claim) gets the
``60/minute`` cap. This test drives both through ``POST /student/ask`` with
a stubbed tutor service and asserts:

* the free-tier caller is 429'd on the 11th call (cap = 10), and
* the premium-tier caller sails past 10 calls without a 429.

Free and premium callers land in different limiter buckets because the
limiter key is prefixed with the resolved tier, so the two halves of the
test don't contaminate each other's counters. We still ``limiter.reset()``
between them for good measure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_tutor_service
from app.core.rate_limit import LLM_LIMIT, limiter, llm_limit_for_key, tier_llm_limits
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


_BODY = {
    "question": "What is photosynthesis?",
    "student_class": "Primary 5",
    "subject": "biology",
}


def _free_cap() -> int:
    return int(LLM_LIMIT.split("/", 1)[0])


def test_free_tier_is_capped_at_the_default_llm_limit(stubbed_tutor_client):
    cap = _free_cap()
    statuses = [
        stubbed_tutor_client.post("/api/v1/student/ask", json=_BODY).status_code
        for _ in range(cap + 2)
    ]
    assert statuses[:cap] == [200] * cap, statuses
    assert statuses[cap] == 429, statuses


def test_premium_tier_gets_a_higher_cap_than_free(stubbed_tutor_client):
    cap = _free_cap()
    # Fire more than the free cap; premium (60/minute) must let them all
    # through where free would have 429'd at call #(cap + 1).
    headers = {"X-Dev-Tier": "premium"}
    statuses = [
        stubbed_tutor_client.post(
            "/api/v1/student/ask", json=_BODY, headers=headers
        ).status_code
        for _ in range(cap + 3)
    ]
    assert all(s == 200 for s in statuses), (
        f"Premium tier should not be rate-limited at {cap + 3} calls, got {statuses}"
    )


def test_premium_and_free_callers_use_separate_buckets(stubbed_tutor_client):
    cap = _free_cap()
    # Exhaust the free bucket first.
    for _ in range(cap + 1):
        stubbed_tutor_client.post("/api/v1/student/ask", json=_BODY)
    free_after = stubbed_tutor_client.post("/api/v1/student/ask", json=_BODY)
    assert free_after.status_code == 429

    # A premium caller, same client/IP, is unaffected by the free 429.
    premium = stubbed_tutor_client.post(
        "/api/v1/student/ask", json=_BODY, headers={"X-Dev-Tier": "premium"}
    )
    assert premium.status_code == 200


def test_llm_limit_for_key_maps_tier_prefix_to_configured_limit():
    limits = tier_llm_limits()
    assert llm_limit_for_key(f"premium|ip:1.2.3.4") == limits["premium"]
    assert llm_limit_for_key(f"free|ip:1.2.3.4") == limits["free"]
    # Unknown tier falls back to the default tier's limit (free by default).
    assert llm_limit_for_key("mystery|ip:1.2.3.4") == limits["free"]
    # A key with no tier separator falls back to the default tier too.
    assert llm_limit_for_key("ip:1.2.3.4") == limits["free"]
