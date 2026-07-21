"""Guardrail helpers for grounded QA outputs."""

from .content_filter import (
    contains_forbidden_source_language,
    contains_internal_debug_language,
    sanitize_student_answer,
)
from .hallucination_checker import has_large_verbatim_overlap
from .output_formatter import ensure_teacherly_close, strip_numbered_formatting
from .role_validator import (
    RoleValidationResult,
    output_violates_role,
    validate_query_for_role,
)

__all__ = [
    "RoleValidationResult",
    "contains_forbidden_source_language",
    "contains_internal_debug_language",
    "ensure_teacherly_close",
    "has_large_verbatim_overlap",
    "output_violates_role",
    "sanitize_student_answer",
    "strip_numbered_formatting",
    "validate_query_for_role",
]
