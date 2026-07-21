"""Shared API response schemas used across every role."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

ResponseStatus = Literal["success", "error"]
UserRole = Literal["student", "teacher", "parent", "admin", "system"]

DataT = TypeVar("DataT")


class APIResponse(BaseModel, Generic[DataT]):
    """Standard response envelope enforced across every endpoint.

    Every API response in RevEd must conform to this shape:
    ``{"status", "data", "message", "role"}``.
    """

    status: ResponseStatus = Field(..., description="Outcome of the request.")
    data: DataT | None = Field(
        default=None,
        description="Payload returned on success. Null on error.",
    )
    message: str = Field(
        default="",
        description="Human-readable message suitable for display.",
    )
    role: UserRole = Field(..., description="The user role scope of this response.")


class ErrorData(BaseModel):
    """Structured error payload returned inside the envelope on failure."""

    code: str = Field(..., description="Machine-readable error code.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured debug details.",
    )


# --- Shared AI-generation list/detail schemas (any role) -----------------


class AIGenerationSummary(BaseModel):
    """One row in any role's recent-generations list."""

    generation_id: uuid.UUID
    generation_type: str
    role: UserRole
    title: str
    subject: str | None = None
    student_class: str | None = None
    topic: str | None = None
    conversation_id: uuid.UUID | None = None
    sources: list[str] = Field(default_factory=list)
    created_at: datetime


class AIGenerationListResponse(BaseModel):
    generations: list[AIGenerationSummary] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque pagination cursor. Pass it back as ``?cursor=...`` on "
            "the next request to fetch the page after these results. "
            "``null`` means this is the last page."
        ),
    )


class AIGenerationDetail(BaseModel):
    """Full request + response payloads for a single persisted generation."""

    generation_id: uuid.UUID
    generation_type: str
    role: UserRole
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
