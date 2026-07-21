"""Pydantic schemas for all student-facing endpoints."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.utils.subjects import VALID_SUBJECTS

# --- Allowed values ---
#
# The canonical subject set lives in ``app.utils.subjects``;
# ``VALID_SUBJECTS`` is re-exported here for backwards compatibility.
# Student request validators keep the strict canonical-set check (these
# endpoints aren't part of the free-text frontend surface, and the strict
# gate is asserted by the pen-test suite). The tolerant ``coerce_subject``
# path is used by the teacher/parent generation surfaces the frontend
# actually posts free-text subjects to.

_CLASS_PATTERN = re.compile(
    r"^(Primary\s+[1-6]|JSS[1-3]|SS[1-3])$",
    re.IGNORECASE,
)


# --- Request ---


class ConversationTurn(BaseModel):
    """One prior conversation message supplied by the frontend."""

    role: Literal["user", "assistant"] = Field(
        ...,
        description="The speaker for the prior message.",
        examples=["user", "assistant"],
    )
    content: str = Field(
        ...,
        min_length=1,
        description="The text content of the prior message.",
    )

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value


class LearningState(BaseModel):
    """Optional learner progress signals used to adapt tutoring style."""

    understanding_level: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="Estimated current understanding of the topic.",
    )
    previous_attempt_correct: bool | None = Field(
        default=None,
        description="Whether the student's previous attempt was correct.",
    )
    attempt_count: int | None = Field(
        default=None,
        ge=0,
        description="How many attempts the student has made so far.",
    )


class StudentQuestionRequest(BaseModel):
    """Incoming student question payload from the frontend."""

    question: str = Field(
        ...,
        min_length=1,
        description="The student's question text.",
        examples=["What is photosynthesis?"],
    )
    student_class: str = Field(
        ...,
        description="Learner level, e.g. 'Primary 5', 'JSS1', 'SS2'.",
        examples=["Primary 5", "JSS1", "SS2"],
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Optional subject hint. Typos and short forms (e.g. 'bio', 'chemstry') "
            "are auto-corrected by the backend to 'biology', 'chemistry', or 'physics'."
        ),
        examples=["biology", "physics", "bio", "chemstry"],
    )
    history: list[ConversationTurn] | None = Field(
        default=None,
        description=(
            "Optional prior conversation turns for continuity. The frontend "
            "may either keep ``history`` client-side and replay it on every "
            "request (stateless mode), or pass ``conversation_id`` and let "
            "the backend rehydrate prior turns from ``chat_messages`` via "
            "``GET /student/conversations/{id}/history``."
        ),
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional thread identifier. If provided, this turn is recorded "
            "under the same conversation as prior /ask calls. If omitted, the "
            "backend generates a new conversation_id and returns it in the "
            "response so the frontend can carry it forward."
        ),
    )
    learning_state: LearningState | None = Field(
        default=None,
        description="Optional learner progress signals for adaptive tutoring.",
    )

    @field_validator("question", mode="before")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("student_class")
    @classmethod
    def _validate_student_class(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLASS_PATTERN.match(normalized):
            raise ValueError(
                f"Invalid student_class '{normalized}'. "
                "Must be one of: Primary 1-6, JSS1-3, SS1-3."
            )
        return normalized

    @field_validator("subject", mode="before")
    @classmethod
    def _normalize_subject_hint(cls, value: str | None) -> str | None:
        """Accept any non-empty string; autocorrection happens downstream."""

        if value is None:
            return None
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @field_validator("history")
    @classmethod
    def _normalize_history(cls, value: list[ConversationTurn] | None) -> list[ConversationTurn] | None:
        if not value:
            return None
        return value

    @field_validator("learning_state")
    @classmethod
    def _normalize_learning_state(cls, value: LearningState | None) -> LearningState | None:
        if value is None:
            return None
        if (
            value.understanding_level is None
            and value.previous_attempt_correct is None
            and value.attempt_count is None
        ):
            return None
        return value


# --- Response payloads ---


class StudentAnswerResponse(BaseModel):
    """Frontend-facing answer payload.

    When the tutor needed to ask a clarifying question instead of answering,
    ``status`` is ``"needs_clarification"``, ``answer`` is empty, and
    ``clarifying_question`` contains the prompt to show the student.

    When the question was understood, ``status`` is ``"answered"``. If the
    tutor auto-corrected the student's input, ``original_question`` and
    ``corrected_question`` both appear so the UI can show "Did you mean …?".
    """

    status: Literal["answered", "needs_clarification"] = Field(
        default="answered",
        description=(
            "'answered' when the tutor responded with a full answer. "
            "'needs_clarification' when the tutor asks the student to clarify."
        ),
    )
    answer: str = Field(
        default="",
        description=(
            "The teacher-style grounded answer text. Empty when "
            "status='needs_clarification'."
        ),
    )
    student_class: str = Field(
        ...,
        description="The student class level used for this answer.",
    )
    subject: str | None = Field(
        default=None,
        description="Subject filter that was applied, if any.",
    )
    original_question: str | None = Field(
        default=None,
        description="The student's raw question, populated only if it was auto-corrected.",
    )
    corrected_question: str | None = Field(
        default=None,
        description="The cleaned-up question the tutor actually answered, if corrected.",
    )
    original_subject: str | None = Field(
        default=None,
        description="The student's raw subject hint, populated only if it was auto-corrected.",
    )
    clarifying_question: str | None = Field(
        default=None,
        description=(
            "A short clarifying question from the tutor to show the student. "
            "Non-null only when status='needs_clarification'."
        ),
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Thread identifier this turn belongs to. The frontend should "
            "pass this back on the next /ask call to continue the same "
            "conversation. Always non-null when chat persistence is "
            "configured; may be null in dev mode without a database."
        ),
    )


# --- Conversation thread browsing ---


class ConversationSummary(BaseModel):
    """One row in the user's conversation list."""

    conversation_id: uuid.UUID
    subject: str | None = Field(
        default=None,
        description="Subject filter used for the most recent message in this thread, if any.",
    )
    message_count: int = Field(..., description="Number of stored turns in the thread.")
    last_question_preview: str = Field(
        default="",
        description="First ~120 characters of the most recent question for UI listing.",
    )
    started_at: datetime = Field(..., description="When the first turn was recorded.")
    last_active_at: datetime = Field(..., description="When the most recent turn was recorded.")


class ConversationListResponse(BaseModel):
    """Wrapper for the conversation list endpoint."""

    conversations: list[ConversationSummary] = Field(default_factory=list)


class ConversationHistoryResponse(BaseModel):
    """Ordered turns ready to plug into the next /ask request as ``history``."""

    conversation_id: uuid.UUID
    turns: list[ConversationTurn] = Field(default_factory=list)


# --- Learning Pathway ---


class LearningPathRequest(BaseModel):
    """Request payload for generating a personalized learning pathway."""

    student_class: str = Field(..., examples=["JSS2", "SS1"])
    subject: str = Field(..., examples=["biology"])
    topic: str = Field(
        ...,
        min_length=2,
        description="The topic or syllabus area the learner wants to master.",
        examples=["Cell structure and function"],
    )
    current_understanding: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="Self-reported current understanding of the topic.",
    )
    weekly_study_hours: int | None = Field(
        default=None,
        ge=1,
        le=40,
        description="Hours the student can dedicate per week.",
    )

    @field_validator("student_class")
    @classmethod
    def _validate_student_class(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLASS_PATTERN.match(normalized):
            raise ValueError("student_class must be Primary 1-6, JSS1-3, or SS1-3.")
        return normalized

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_SUBJECTS:
            raise ValueError(
                f"subject must be one of: {', '.join(sorted(VALID_SUBJECTS))}."
            )
        return normalized


class LearningPathStep(BaseModel):
    """One ordered step in a generated learning pathway."""

    order: int = Field(..., ge=1)
    title: str
    focus: str = Field(..., description="What the learner will practice or master.")
    suggested_activity: str = Field(
        ...,
        description="Concrete activity the learner should do.",
    )
    estimated_hours: float = Field(..., ge=0.25, le=20.0)


class LearningPathResponse(BaseModel):
    """Generated personalized learning pathway."""

    topic: str
    subject: str
    student_class: str
    overview: str = Field(..., description="Teacher-style summary of the path.")
    steps: list[LearningPathStep]
    encouragement: str = Field(
        default="",
        description="A short motivational closing note for the learner.",
    )


# --- Goals ---


class GoalCreateRequest(BaseModel):
    """Create a new learning goal for a student."""

    student_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    subject: str | None = None
    target_date: date | None = None

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in VALID_SUBJECTS:
            raise ValueError(
                f"subject must be one of: {', '.join(sorted(VALID_SUBJECTS))}."
            )
        return normalized


class GoalProgressUpdateRequest(BaseModel):
    """Update progress on an existing goal."""

    progress_percent: int = Field(..., ge=0, le=100)
    note: str | None = Field(default=None, max_length=500)


class GoalResponse(BaseModel):
    """Serialized goal payload."""

    id: str
    student_id: str
    title: str
    description: str | None
    subject: str | None
    target_date: date | None
    progress_percent: int
    status: Literal["active", "completed"]
    coaching_note: str = Field(
        default="",
        description="AI-generated encouragement and next-step suggestion.",
    )
    created_at: datetime
    updated_at: datetime


class GoalListResponse(BaseModel):
    """List of goals for a student."""

    student_id: str
    goals: list[GoalResponse]


# --- Study Groups ---


class StudyGroupCreateRequest(BaseModel):
    """Create a new collaborative study group."""

    creator_student_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=2, max_length=80)
    subject: str
    topic: str = Field(..., min_length=2, max_length=120)
    student_class: str

    @field_validator("subject")
    @classmethod
    def _validate_subject(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_SUBJECTS:
            raise ValueError(
                f"subject must be one of: {', '.join(sorted(VALID_SUBJECTS))}."
            )
        return normalized

    @field_validator("student_class")
    @classmethod
    def _validate_student_class(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLASS_PATTERN.match(normalized):
            raise ValueError("student_class must be Primary 1-6, JSS1-3, or SS1-3.")
        return normalized


class StudyGroupJoinRequest(BaseModel):
    """Join an existing study group."""

    student_id: str = Field(..., min_length=1)


class StudyGroupDiscussionRequest(BaseModel):
    """Ask the AI facilitator to guide a group discussion."""

    focus_question: str = Field(..., min_length=3, max_length=400)


class StudyGroupResponse(BaseModel):
    """Serialized study group payload."""

    id: str
    name: str
    subject: str
    topic: str
    student_class: str
    creator_student_id: str
    member_student_ids: list[str]
    created_at: datetime


class StudyGroupListResponse(BaseModel):
    """List of study groups."""

    groups: list[StudyGroupResponse]


class StudyGroupDiscussionResponse(BaseModel):
    """AI-facilitated discussion prompt set for a study group."""

    group_id: str
    focus_question: str
    opening_prompt: str
    discussion_questions: list[str]
    shared_insight: str = Field(
        default="",
        description="A grounded mini-explanation the facilitator shares with the group.",
    )


# --- Career Guidance ---


class CareerGuidanceRequest(BaseModel):
    """Request AI-driven career guidance for a student."""

    student_class: str
    favorite_subjects: list[str] = Field(..., min_length=1, max_length=10)
    strengths: list[str] = Field(default_factory=list, max_length=10)
    interests: list[str] = Field(default_factory=list, max_length=10)
    long_term_dream: str | None = Field(default=None, max_length=400)

    @field_validator("student_class")
    @classmethod
    def _validate_student_class(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLASS_PATTERN.match(normalized):
            raise ValueError("student_class must be Primary 1-6, JSS1-3, or SS1-3.")
        return normalized


class CareerSuggestion(BaseModel):
    """A single career suggestion with rationale and next steps."""

    career: str
    why_it_fits: str
    recommended_subjects: list[str]
    next_steps: list[str]


class CareerGuidanceResponse(BaseModel):
    """Generated career guidance payload."""

    student_class: str
    overview: str
    suggestions: list[CareerSuggestion]
    encouragement: str = ""
