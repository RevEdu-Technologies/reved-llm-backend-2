"""Pydantic schemas for parent-facing endpoints.

The parent persona treats the AI as a *home-learning companion* — it
explains topics in plain everyday language so a parent without subject
expertise can help their child. Parents see only ``student_ok`` material
(visibility filter handled by the retriever's ``role='parent'`` flag).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

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
    s = value.strip()
    if not _CLASS_PATTERN.match(s):
        raise ValueError("student_class must be one of: Primary 1-6, JSS1-3, SS1-3.")
    return s


# --- Explain topic --------------------------------------------------------


class ExplainTopicRequest(BaseModel):
    """Ask the AI to explain a topic so the parent can help their child."""

    subject: str = Field(..., examples=["biology", "mathematics"])
    student_class: str = Field(
        ...,
        examples=["Primary 5", "JSS2", "SS1"],
        description="The child's class — the explanation is tuned to this level.",
    )
    topic: str = Field(
        ...,
        min_length=2,
        examples=["Photosynthesis", "Fractions"],
        description="What the parent wants explained.",
    )
    child_question: str | None = Field(
        default=None,
        description=(
            "Optional: the specific question the child asked at home, so the "
            "AI can address it directly instead of giving a generic overview."
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


class ExplainTopicResponse(BaseModel):
    topic: str
    subject: str
    student_class: str
    explanation: str = Field(
        default="",
        description="2-4 paragraphs of plain-language explanation suitable for a non-expert parent.",
    )
    everyday_analogy: str = Field(
        default="",
        description="A real-world analogy the parent can use to walk the child through the idea.",
    )
    things_to_try_at_home: list[str] = Field(
        default_factory=list,
        description="Practical activities the parent can do with the child.",
    )
    sources: list[str] = Field(default_factory=list)


# --- Child activity -------------------------------------------------------


class ChildActivitySummary(BaseModel):
    """Per-child recent activity rollup."""

    student_id: uuid.UUID
    student_name: str
    grade_level: str | None = None
    period_start: datetime
    period_end: datetime
    total_questions: int = Field(default=0)
    questions_by_subject: dict[str, int] = Field(default_factory=dict)
    recent_questions: list[str] = Field(
        default_factory=list,
        description="Up to 10 recent question previews — what the child is asking the tutor.",
    )


class ChildActivityResponse(BaseModel):
    """Aggregate the parent's children's recent activity.

    A parent may be linked to multiple children via ``students.parent_id``
    pointing at this parent's row. If no link exists, returns an empty list.
    """

    parent_user_id: str
    children: list[ChildActivitySummary] = Field(default_factory=list)
    note: str = Field(
        default=(
            "Phase 1 view. Per-subject mastery and timing land once exercise "
            "tracking is captured upstream."
        ),
    )
