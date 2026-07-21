"""Unit tests for the student tutor service layer."""

from __future__ import annotations

import asyncio

import pytest

from app.rag.query_engine.engine import GroundedQAEngine
from app.rag.query_engine.router import RoleAwareQueryRouter
from app.rag.retrieval.retriever import RetrievalResult
from app.schemas.student import ConversationTurn, LearningState
from app.services.student.tutor_service import StudentTutorService, TutorAnswer


class FakeRetriever:
    """Stub retriever that returns canned results."""

    def __init__(self, retrieval_results):
        self.retrieval_results = retrieval_results

    def retrieve(self, query_text, *, top_k=5, subject=None, namespace=None):
        return self.retrieval_results


class FakeLLMClient:
    """Stub LLM client that returns a static answer."""

    def __init__(self, answer_text: str = "Plants use sunlight to make food."):
        self._answer_text = answer_text

    def generate(self, *, system_prompt, user_prompt):
        return type("FakeResponse", (), {"text": self._answer_text})


def _make_service(
    *,
    answer_text: str = "Plants use sunlight to make food.",
    retrieval_score: float = 0.9,
) -> StudentTutorService:
    """Build a service with fake retriever and LLM."""

    retrieval_results = [
        RetrievalResult(
            score=retrieval_score,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=1,
            text="Photosynthesis converts sunlight into food in plant leaves.",
        )
    ]
    engine = GroundedQAEngine(
        retriever=FakeRetriever(retrieval_results),
        llm_client=FakeLLMClient(answer_text),
    )
    return StudentTutorService(router=RoleAwareQueryRouter(engine=engine))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_ask_returns_tutor_answer():
    service = _make_service()
    result = _run(
        service.ask(
            question="What is photosynthesis?",
            student_class="Primary 5",
            subject="biology",
        )
    )

    assert isinstance(result, TutorAnswer)
    assert result.student_class == "Primary 5"
    assert result.subject == "biology"
    assert "sunlight" in result.answer


def test_ask_hides_internal_data():
    """TutorAnswer must not expose sources, chunks, or scores."""

    service = _make_service()
    result = _run(
        service.ask(
            question="What is photosynthesis?",
            student_class="JSS1",
        )
    )

    assert not hasattr(result, "sources")
    assert not hasattr(result, "retrieved_chunks")
    assert not hasattr(result, "used_subject_filter")


def test_ask_without_subject():
    service = _make_service()
    result = _run(
        service.ask(
            question="What is energy?",
            student_class="SS2",
        )
    )
    assert result.subject is None


def test_ask_insufficient_retrieval_returns_fallback():
    service = _make_service(retrieval_score=0.1)
    result = _run(
        service.ask(
            question="What is quantum entanglement in physics?",
            student_class="SS3",
            subject="physics",
        )
    )
    assert "enough information" in result.answer.lower()


def test_ask_accepts_follow_up_history():
    service = _make_service()
    result = _run(
        service.ask(
            question="Explain it more simply.",
            student_class="Primary 5",
            subject="biology",
            history=[
                ConversationTurn(role="user", content="What is photosynthesis?"),
                ConversationTurn(role="assistant", content="It is how plants make food."),
            ],
        )
    )

    assert isinstance(result, TutorAnswer)
    assert result.student_class == "Primary 5"
    assert result.subject == "biology"


def test_ask_accepts_learning_state():
    service = _make_service()
    result = _run(
        service.ask(
            question="What is photosynthesis?",
            student_class="Primary 5",
            subject="biology",
            learning_state=LearningState(
                understanding_level="low",
                previous_attempt_correct=False,
                attempt_count=2,
            ),
        )
    )

    assert isinstance(result, TutorAnswer)
    assert result.student_class == "Primary 5"
    assert result.subject == "biology"


def test_ask_rejects_non_educational_query():
    service = _make_service()
    result = _run(
        service.ask(
            question="How do I hack a system?",
            student_class="SS1",
        )
    )
    assert "outside" in result.answer.lower() or "educational" in result.answer.lower()


def test_ask_strips_internal_debug_language_from_public_answer():
    service = _make_service(
        answer_text="Retrieved chunk score 0.91 from source biology.txt provides the context for photosynthesis."
    )
    result = _run(
        service.ask(
            question="What is photosynthesis?",
            student_class="Primary 5",
            subject="biology",
        )
    )

    lowered = result.answer.lower()
    assert "chunk" not in lowered
    assert "score" not in lowered
    assert "retrieved" not in lowered
