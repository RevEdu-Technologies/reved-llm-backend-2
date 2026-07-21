"""Unit tests for student QA schemas - validation rules."""

import pytest
from pydantic import ValidationError

from app.schemas.student import (
    ConversationTurn,
    LearningState,
    StudentAnswerResponse,
    StudentQuestionRequest,
)


class TestConversationTurnValidation:
    """Validate individual history turn rules."""

    def test_valid_turn_is_accepted(self):
        turn = ConversationTurn(role="user", content=" Explain it more simply. ")
        assert turn.role == "user"
        assert turn.content == "Explain it more simply."

    def test_invalid_role_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ConversationTurn(role="system", content="Hidden prompt")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("role",) for e in errors)

    def test_empty_content_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            ConversationTurn(role="assistant", content="   ")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("content",) for e in errors)


class TestLearningStateValidation:
    """Validate adaptive tutoring state rules."""

    def test_valid_learning_state_is_accepted(self):
        state = LearningState(
            understanding_level="medium",
            previous_attempt_correct=True,
            attempt_count=1,
        )
        assert state.understanding_level == "medium"
        assert state.previous_attempt_correct is True
        assert state.attempt_count == 1

    def test_invalid_understanding_level_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            LearningState(understanding_level="expert")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("understanding_level",) for e in errors)

    def test_negative_attempt_count_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            LearningState(attempt_count=-1)
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("attempt_count",) for e in errors)


class TestStudentQuestionRequestValidation:
    """Validate request schema rules for question, student_class, subject, history, and learning state."""

    def test_valid_request_minimal(self):
        req = StudentQuestionRequest(
            question="What is photosynthesis?",
            student_class="JSS1",
        )
        assert req.question == "What is photosynthesis?"
        assert req.student_class == "JSS1"
        assert req.subject is None
        assert req.history is None
        assert req.learning_state is None

    def test_valid_request_with_subject_history_and_learning_state(self):
        req = StudentQuestionRequest(
            question="Give another example.",
            student_class="SS2",
            subject="physics",
            history=[
                {"role": "user", "content": "Explain Newton's first law."},
                {"role": "assistant", "content": "It means an object keeps doing what it is doing."},
            ],
            learning_state={
                "understanding_level": "low",
                "previous_attempt_correct": False,
                "attempt_count": 2,
            },
        )
        assert req.subject == "physics"
        assert req.history is not None
        assert len(req.history) == 2
        assert req.learning_state is not None
        assert req.learning_state.understanding_level == "low"

    def test_question_whitespace_is_trimmed(self):
        req = StudentQuestionRequest(
            question="  What is energy?  ",
            student_class="JSS2",
        )
        assert req.question == "What is energy?"

    def test_empty_question_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            StudentQuestionRequest(question="", student_class="JSS1")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("question",) for e in errors)

    def test_whitespace_only_question_is_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            StudentQuestionRequest(question="   ", student_class="JSS1")
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("question",) for e in errors)

    @pytest.mark.parametrize(
        "student_class",
        [
            "Primary 1",
            "Primary 2",
            "Primary 3",
            "Primary 4",
            "Primary 5",
            "Primary 6",
            "JSS1",
            "JSS2",
            "JSS3",
            "SS1",
            "SS2",
            "SS3",
        ],
    )
    def test_valid_student_classes_are_accepted(self, student_class: str):
        req = StudentQuestionRequest(
            question="Test?",
            student_class=student_class,
        )
        assert req.student_class == student_class

    @pytest.mark.parametrize(
        "student_class",
        [
            "Primary 0",
            "Primary 7",
            "Primary7",
            "JSS0",
            "JSS4",
            "JS1",
            "SS0",
            "SS4",
            "S1",
            "Grade 5",
            "Year 9",
            "100L",
            "",
        ],
    )
    def test_invalid_student_classes_are_rejected(self, student_class: str):
        with pytest.raises(ValidationError) as exc_info:
            StudentQuestionRequest(
                question="Test?",
                student_class=student_class,
            )
        errors = exc_info.value.errors()
        assert any(e["loc"] == ("student_class",) for e in errors)

    @pytest.mark.parametrize("subject", ["biology", "chemistry", "physics"])
    def test_valid_subjects_are_accepted(self, subject: str):
        req = StudentQuestionRequest(
            question="Test?",
            student_class="JSS1",
            subject=subject,
        )
        assert req.subject == subject

    @pytest.mark.parametrize("subject", ["math", "english", "history", "BIOLOGY ", "bio", "chemstry"])
    def test_non_canonical_subjects_are_passed_through_for_downstream_normalization(
        self, subject: str
    ):
        """The schema accepts any non-empty string; the preflight layer
        normalizes typos/aliases or raises a clarifier downstream."""

        req = StudentQuestionRequest(
            question="Test?",
            student_class="JSS1",
            subject=subject,
        )
        assert req.subject == subject.strip()

    def test_subject_none_is_accepted(self):
        req = StudentQuestionRequest(
            question="Test?",
            student_class="JSS1",
            subject=None,
        )
        assert req.subject is None

    def test_empty_history_is_normalized_to_none(self):
        req = StudentQuestionRequest(
            question="Test?",
            student_class="JSS1",
            history=[],
        )
        assert req.history is None

    def test_empty_learning_state_is_normalized_to_none(self):
        req = StudentQuestionRequest(
            question="Test?",
            student_class="JSS1",
            learning_state={},
        )
        assert req.learning_state is None


class TestStudentAnswerResponse:
    """Validate response schema shape."""

    def test_response_shape(self):
        resp = StudentAnswerResponse(
            answer="Plants use sunlight to make food.",
            student_class="Primary 5",
            subject="biology",
        )
        data = resp.model_dump()
        # ``conversation_id`` joined the schema when chat persistence
        # landed — the frontend echoes it on the next /ask call to
        # continue the same thread.
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
        assert data["status"] == "answered"

    def test_response_without_subject(self):
        resp = StudentAnswerResponse(
            answer="Energy is the ability to do work.",
            student_class="JSS2",
        )
        assert resp.subject is None

    def test_response_does_not_expose_internal_fields(self):
        fields = set(StudentAnswerResponse.model_fields.keys())
        assert "sources" not in fields
        assert "retrieved_chunks" not in fields
        assert "debug" not in fields
        assert "top_k" not in fields
        assert "score" not in fields
