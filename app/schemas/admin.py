"""Pydantic schemas for admin endpoints.

Admins manage the platform's institutional data: schools, teachers,
parents, students, and the teacher↔class mappings that downstream features
(class progress, parent-child views) depend on.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# --- Teacher provisioning -------------------------------------------------


class ClassSpec(BaseModel):
    """One class the teacher will teach. Used by /admin/teachers/setup."""

    name: str = Field(..., examples=["SS2A"], description="Class label.")
    subject: str | None = Field(
        default=None,
        examples=["physics", "english_language"],
        description="Subject this class focuses on (snake_case canonical form).",
    )
    grade_level: str | None = Field(
        default=None,
        examples=["SS1", "SS2", "SS3", "JSS3"],
        description="Grade level. Should match student_class values used elsewhere.",
    )

    @field_validator("subject", mode="before")
    @classmethod
    def _norm(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip().lower()
        return s or None


class TeacherSetupRequest(BaseModel):
    """One-shot provisioning for a teacher (school + teacher row + classes)."""

    supabase_user_id: uuid.UUID = Field(
        ...,
        description=(
            "The Supabase auth user_id to link. In dev mode pass the stub id "
            "(00000000-0000-0000-0000-000000000001)."
        ),
    )
    full_name: str = Field(..., min_length=2)
    email: str | None = Field(default=None)
    subject_specialty: str | None = Field(default=None)
    school_name: str = Field(..., min_length=2)
    school_country: str | None = Field(default=None)
    classes: list[ClassSpec] = Field(
        default_factory=list,
        description=(
            "Classes the teacher will teach. The /class-progress endpoint "
            "filters student questions by these (subject, grade_level) pairs."
        ),
    )


class TeacherSetupResponse(BaseModel):
    school_id: uuid.UUID
    teacher_id: uuid.UUID
    class_ids: list[uuid.UUID] = Field(default_factory=list)
    linked_user_id: uuid.UUID
    message: str = Field(default="Teacher provisioned.")


# --- Parent provisioning --------------------------------------------------


class StudentSpec(BaseModel):
    """A child belonging to the parent being provisioned."""

    full_name: str = Field(..., min_length=2)
    grade_level: str | None = Field(default=None, examples=["JSS2", "SS1"])
    email: str | None = Field(default=None)
    supabase_user_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional: if this child already has a Supabase auth account, link "
            "it. Otherwise the student row is created without an auth link."
        ),
    )


class ParentSetupRequest(BaseModel):
    """One-shot provisioning for a parent (parent row + linked children)."""

    supabase_user_id: uuid.UUID = Field(
        ...,
        description="The Supabase auth user_id to link the Parent row to.",
    )
    full_name: str = Field(..., min_length=2)
    email: str | None = Field(default=None)
    phone: str | None = Field(default=None)
    children: list[StudentSpec] = Field(
        default_factory=list,
        description=(
            "Children this parent is responsible for. Each becomes a Student "
            "row with parent_id pointing at this parent."
        ),
    )


class ParentSetupResponse(BaseModel):
    parent_id: uuid.UUID
    linked_user_id: uuid.UUID
    student_ids: list[uuid.UUID] = Field(default_factory=list)
    message: str = Field(default="Parent provisioned.")


# --- Platform stats -------------------------------------------------------


class UsageSummaryResponse(BaseModel):
    """Coarse platform-wide usage view for admins."""

    period_start: datetime
    period_end: datetime
    total_student_questions: int = Field(default=0)
    total_ai_generations: int = Field(
        default=0,
        description="All AI generations in the period across teacher/student/parent.",
    )
    generations_by_role: dict[str, int] = Field(
        default_factory=dict,
        description="Generation counts broken down by caller role.",
    )
    questions_by_subject: dict[str, int] = Field(default_factory=dict)
    generations_by_type: dict[str, int] = Field(default_factory=dict)
    distinct_student_users: int = Field(default=0)
    distinct_generating_users: int = Field(
        default=0,
        description="Distinct users who produced any AI generation in the period.",
    )
    schools: int = Field(default=0)
    teachers: int = Field(default=0)
    parents: int = Field(default=0)
    students: int = Field(default=0)


class ClassRosterRequest(BaseModel):
    """Assign a roster of students to one class."""

    student_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Existing Student.id values to enrol in the class.",
    )
    student_supabase_user_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description=(
            "Convenience alternative: supabase auth user ids. Each is resolved "
            "to a Student row; rows that don't exist are skipped."
        ),
    )


class ClassRosterResponse(BaseModel):
    class_id: uuid.UUID
    added: list[uuid.UUID] = Field(
        default_factory=list,
        description="Student ids successfully added (or already present).",
    )
    skipped_user_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Supabase user ids that had no matching Student row.",
    )
    total_in_class: int = Field(
        default=0, description="Number of students in the class after this call."
    )
    message: str = Field(default="Roster updated.")


class ContentStatsResponse(BaseModel):
    """Corpus statistics (Pinecone + JSONL on disk)."""

    pinecone_index: str
    pinecone_namespace: str
    pinecone_dimension: int
    pinecone_vector_count: int = Field(default=0)
    on_disk_chunk_files: int = Field(default=0)
    on_disk_chunks_total: int = Field(default=0)
    chunks_by_content_type: dict[str, int] = Field(default_factory=dict)
    chunks_by_subject: dict[str, int] = Field(default_factory=dict)
