"""Pydantic schemas for all teacher-facing endpoints.

The teacher API treats the AI as a *teaching assistant* — it produces lesson
notes, quiz drafts, marking guides, and per-student feedback grounded in the
same corpus the student tutor reads from, but with ``role="teacher"`` so the
retrieval layer also returns ``teacher_only`` chunks (marking schemes,
teacher-guide content).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.utils.subjects import coerce_subject

_CLASS_PATTERN = re.compile(r"^(Primary\s+[1-6]|JSS[1-3]|SS[1-3])$", re.IGNORECASE)


def _normalize_subject(value: str | None) -> str | None:
    # Map free-text / umbrella subjects from the frontend to a canonical
    # value (or "general") so retrieval filters correctly; never 422.
    if value is None or not value.strip():
        return None
    return coerce_subject(value)


def _validate_class(value: str) -> str:
    normalized = value.strip()
    if not _CLASS_PATTERN.match(normalized):
        raise ValueError(
            "student_class must be one of: Primary 1-6, JSS1-3, SS1-3."
        )
    return normalized


def grade_level_to_class(value: int | str) -> str:
    """Map a numeric grade level to a canonical RevEd ``student_class``.

    The frontend stores grade levels as integers (see the
    ``reved-technologies`` ``classSections.ts`` source of truth):

        1-6   -> Primary 1 .. Primary 6   (primary)
        7-9   -> JSS1 .. JSS3             (junior secondary)
        10-12 -> SS1 .. SS3              (senior secondary)

    Already-canonical strings (``"JSS2"``, ``"Primary 5"``) pass through.
    Out-of-range numbers clamp to the nearest valid class so a stray value
    never produces a ``422`` on this compatibility surface.
    """

    # Already a canonical class string? Accept as-is.
    if isinstance(value, str):
        stripped = value.strip()
        if _CLASS_PATTERN.match(stripped):
            # Normalize spacing/case to the canonical form.
            return _normalize_class_caps(stripped)
        # A numeric-looking string ("9") — fall through to int handling.
        try:
            value = int(stripped)
        except (TypeError, ValueError):
            raise ValueError(
                "grade_level must be a number (1-12) or a class like "
                "'Primary 5', 'JSS2', 'SS1'."
            )

    grade = int(value)
    if grade <= 6:
        return f"Primary {max(grade, 1)}"
    if grade <= 9:
        return f"JSS{grade - 6}"
    return f"SS{min(grade - 9, 3)}"


def _normalize_class_caps(value: str) -> str:
    """Title-case a matched class string to the canonical capitalization."""

    upper = value.upper().replace(" ", "")
    if upper.startswith("JSS"):
        return f"JSS{upper[3:]}"
    if upper.startswith("SS"):
        return f"SS{upper[2:]}"
    # Primary N
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"Primary {digits}"


# --- Frontend-compatible content generation -------------------------------
#
# The RevEd web app already posts to a `generate-lesson-content` function
# with camelCase fields, a *numeric* grade level, and `learningObjectives`
# as a single free-text string, and consumes an OpenAI-style markdown SSE
# stream. `TeacherContentRequest` mirrors that exact payload so the
# frontend can repoint at this backend (which adds RAG grounding) with no
# request changes. See `POST /teacher/generate-content`.

ContentType = Literal["lesson_plan", "quiz", "notes", "slides", "study_guide"]
DifficultyLevel = Literal["beginner", "intermediate", "advanced"]
ContentTone = Literal["professional", "engaging", "simplified"]


class TeacherContentRequest(BaseModel):
    """Markdown content generation request (frontend payload shape)."""

    contentType: ContentType = Field(
        ...,
        description="What artefact to produce.",
        examples=["lesson_plan", "quiz", "notes", "slides", "study_guide"],
    )
    subject: str = Field(..., examples=["Mathematics", "biology", "Science"])
    gradeLevel: int | str = Field(
        ...,
        description=(
            "Numeric grade level 1-12 (1-6 Primary, 7-9 JSS, 10-12 SS) or a "
            "canonical class string like 'JSS2'. Mapped to student_class."
        ),
        examples=[9, "SS1"],
    )
    topic: str = Field(..., min_length=2, examples=["Photosynthesis"])
    learningObjectives: str | None = Field(
        default=None,
        description="Optional free-text objectives / extra instructions.",
    )
    difficultyLevel: DifficultyLevel = Field(default="intermediate")
    curriculumStandard: str | None = Field(
        default=None,
        description="Optional curriculum standard to align to (e.g. WAEC, NERDC).",
    )
    tone: ContentTone = Field(default="engaging")

    @field_validator("subject", mode="before")
    @classmethod
    def _norm_subject(cls, v: str | None) -> str:
        if v is None or not str(v).strip():
            return "general"
        return coerce_subject(str(v))

    @field_validator("gradeLevel")
    @classmethod
    def _validate_grade(cls, v: int | str) -> int | str:
        # Confirm the grade is mappable now (clear 422 on truly bad input)
        # rather than failing later at stream time.
        grade_level_to_class(v)
        return v

    @property
    def student_class(self) -> str:
        """The canonical class string derived from ``gradeLevel``."""

        return grade_level_to_class(self.gradeLevel)


# --- Lesson notes ---------------------------------------------------------


class LessonSection(BaseModel):
    heading: str
    body: str
    examples: list[str] = Field(default_factory=list)


class LessonNotesRequest(BaseModel):
    """Request payload for generating teacher-facing lesson notes."""

    subject: str = Field(..., examples=["physics", "biology", "economics"])
    student_class: str = Field(..., examples=["SS1", "SS2", "JSS3"])
    topic: str = Field(
        ...,
        min_length=2,
        description="The topic / lesson title to draft notes for.",
        examples=["Newton's laws of motion", "Photosynthesis"],
    )
    learning_objectives: list[str] | None = Field(
        default=None,
        description=(
            "Optional explicit learning objectives. If omitted the assistant "
            "infers them from the topic and retrieved curriculum context."
        ),
    )
    duration_minutes: int | None = Field(
        default=None,
        ge=10,
        le=240,
        description="Optional class duration to help the assistant pace the lesson.",
    )
    include_examples: bool = Field(
        default=True,
        description="Whether to weave worked examples into the notes.",
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional grouping id. Sequential generations for the same lesson "
            "plan can share an id so the teacher can browse them together. If "
            "omitted, the backend generates a new id and returns it."
        ),
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _norm_subject(cls, v: str | None) -> str | None:
        return _normalize_subject(v)

    @field_validator("student_class")
    @classmethod
    def _norm_class(cls, v: str) -> str:
        return _validate_class(v)


class LessonNotesResponse(BaseModel):
    """Structured lesson notes the teacher can review or edit."""

    topic: str
    subject: str
    student_class: str
    learning_objectives: list[str] = Field(default_factory=list)
    overview: str = Field(default="")
    sections: list[LessonSection] = Field(default_factory=list)
    teacher_tips: list[str] = Field(default_factory=list)
    misconceptions_to_address: list[str] = Field(default_factory=list)
    sources: list[str] = Field(
        default_factory=list,
        description="Source filenames the notes drew from.",
    )
    generation_id: uuid.UUID | None = Field(
        default=None,
        description="Persisted id of this generation; use with /teacher/generations/{id}.",
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description="Thread id this generation belongs to.",
    )


# --- Quiz generation ------------------------------------------------------


class QuizRequest(BaseModel):
    """Request a quiz blueprint + questions + marking guide."""

    subject: str = Field(..., examples=["physics"])
    student_class: str = Field(..., examples=["SS2"])
    topic: str = Field(..., min_length=2, examples=["Kinematics"])
    num_questions: int = Field(default=10, ge=3, le=30)
    difficulty_mix: dict[str, int] | None = Field(
        default=None,
        description=(
            "Optional counts per difficulty, e.g. {'easy': 4, 'medium': 4, 'hard': 2}. "
            "Sum should match num_questions; otherwise the assistant rebalances."
        ),
        examples=[{"easy": 4, "medium": 4, "hard": 2}],
    )
    question_types: list[Literal["mcq", "short_answer", "numeric", "derivation"]] | None = Field(
        default=None,
        description="Optional restriction on question types.",
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description="Optional thread id (same semantics as on /lesson-notes).",
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _norm_subject(cls, v: str | None) -> str | None:
        return _normalize_subject(v)

    @field_validator("student_class")
    @classmethod
    def _norm_class(cls, v: str) -> str:
        return _validate_class(v)


class QuizQuestion(BaseModel):
    question_number: int
    question: str
    question_type: str = Field(default="short_answer")
    difficulty: Literal["easy", "medium", "hard"] = Field(default="medium")
    options: list[str] | None = Field(
        default=None,
        description="Multiple-choice options. Non-null only for question_type=mcq.",
    )
    marking_guide: str = Field(
        default="",
        description="Expected answer + grading guidance. Teacher-only material.",
    )
    points: int = Field(default=1, ge=1)


class QuizResponse(BaseModel):
    topic: str
    subject: str
    student_class: str
    questions: list[QuizQuestion] = Field(default_factory=list)
    total_points: int = Field(default=0)
    suggested_duration_minutes: int | None = Field(default=None)
    sources: list[str] = Field(default_factory=list)
    generation_id: uuid.UUID | None = Field(default=None)
    conversation_id: uuid.UUID | None = Field(default=None)


# --- Student feedback -----------------------------------------------------


class FeedbackRequest(BaseModel):
    """Generate feedback on a student submission."""

    subject: str = Field(..., examples=["english_language"])
    student_class: str = Field(..., examples=["SS1"])
    question: str = Field(
        ...,
        min_length=2,
        description="The question / prompt the student was given.",
    )
    student_answer: str = Field(
        ...,
        min_length=1,
        description="The student's written response.",
    )
    rubric: str | None = Field(
        default=None,
        description="Optional marking rubric the teacher wants the AI to apply.",
    )
    conversation_id: uuid.UUID | None = Field(
        default=None,
        description="Optional thread id (same semantics as on /lesson-notes).",
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _norm_subject(cls, v: str | None) -> str | None:
        return _normalize_subject(v)

    @field_validator("student_class")
    @classmethod
    def _norm_class(cls, v: str) -> str:
        return _validate_class(v)


class FeedbackResponse(BaseModel):
    overall_score_band: Literal["excellent", "good", "fair", "needs_improvement"] = Field(
        default="fair",
    )
    summary: str = Field(default="")
    strengths: list[str] = Field(default_factory=list)
    areas_for_improvement: list[str] = Field(default_factory=list)
    specific_corrections: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    generation_id: uuid.UUID | None = Field(default=None)
    conversation_id: uuid.UUID | None = Field(default=None)


# --- Class progress -------------------------------------------------------


class ClassProgressResponse(BaseModel):
    """Aggregate class-level activity for the requesting teacher.

    Phase 1 returns activity derived from ``chat_messages`` (student questions
    grouped by subject) — a true per-student mastery view will follow once we
    capture exercise attempts + correctness signals.
    """

    teacher_user_id: str
    period_start: datetime
    period_end: datetime
    total_student_questions: int = Field(default=0)
    questions_by_subject: dict[str, int] = Field(default_factory=dict)
    questions_by_class: dict[str, int] = Field(default_factory=dict)
    top_topics: list[str] = Field(
        default_factory=list,
        description="Most frequent question previews — quick signal of what the class is asking about.",
    )
    note: str = Field(
        default=(
            "Phase 1 progress view. Per-student mastery, exercise attempts, "
            "and time-on-task aren't tracked yet and will be added once those "
            "signals are captured upstream."
        ),
    )
    scope: Literal["teacher_classes", "global_fallback"] = Field(
        default="global_fallback",
        description=(
            "'teacher_classes' when the requesting user is linked to a "
            "Teacher record and we filtered to subjects+grade-levels they "
            "actually teach. 'global_fallback' when no link exists yet "
            "(returns the platform-wide aggregate so the dashboard isn't "
            "empty during onboarding)."
        ),
    )


# --- Persisted generation listing ----------------------------------------


class TeacherGenerationSummary(BaseModel):
    """One row in the teacher's recent-generations list."""

    generation_id: uuid.UUID
    generation_type: Literal["lesson_notes", "quiz", "student_feedback"]
    title: str
    subject: str | None = None
    student_class: str | None = None
    topic: str | None = None
    conversation_id: uuid.UUID | None = None
    sources: list[str] = Field(default_factory=list)
    created_at: datetime


class TeacherGenerationListResponse(BaseModel):
    generations: list[TeacherGenerationSummary] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination cursor. Pass back as ``?cursor=...`` to fetch "
            "the next page. ``null`` on the last page."
        ),
    )


class TeacherGenerationDetail(BaseModel):
    """A persisted generation with its full request + response payloads.

    The frontend uses ``response_payload`` to re-render the original
    artefact without re-running the model.
    """

    generation_id: uuid.UUID
    generation_type: Literal["lesson_notes", "quiz", "student_feedback"]
    title: str
    subject: str | None = None
    student_class: str | None = None
    topic: str | None = None
    conversation_id: uuid.UUID | None = None
    sources: list[str] = Field(default_factory=list)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
