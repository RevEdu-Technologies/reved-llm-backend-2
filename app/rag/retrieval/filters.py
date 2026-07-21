"""Metadata filter helpers for retrieval queries.

The filter builder produces a Pinecone-style dict that can be passed directly
into ``Index.query(filter=...)``. Multiple clauses are combined with ``$and``.
"""

from __future__ import annotations

from typing import Any, Sequence

# Mirrors the canonical set in app.services.student._subject_matcher. The
# retrieval filter rejects unknown subjects so a typo doesn't silently match
# nothing in Pinecone.
SUPPORTED_SUBJECTS = {
    "biology", "chemistry", "physics",
    "mathematics", "further_mathematics",
    "english_language", "literature_in_english",
    "economics", "government", "civic_education",
    "commerce", "accounting", "office_practice",
    "computer", "history", "religious_studies",
    "hausa", "igbo", "yoruba", "general",
}

VALID_VISIBILITY = {"student_ok", "teacher_only"}
VALID_CHUNK_TYPES = {
    "definition", "explanation", "formula", "worked_example", "exercise",
    "solution", "figure", "summary", "misc",
}
# Roles that should only see student_ok material. Parents see the same
# resources their child sees (textbooks, syllabi, etc.) but never teacher-
# exclusive material — same constraint as student.
STUDENT_ROLES = {"student", "parent"}
# Roles that may see everything (teacher_only included).
TEACHER_ROLES = {"teacher", "admin"}


def build_metadata_filter(
    *,
    subject: str | None = None,
    chapter: str | None = None,
    section: str | None = None,
    topic: str | None = None,
    level: str | None = None,
    board: str | None = None,
    chunk_type: str | Sequence[str] | None = None,
    visibility: str | Sequence[str] | None = None,
    role: str | None = None,
    content_type: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a Pinecone metadata filter.

    ``role`` is a high-level shortcut: passing ``role='student'`` forces
    ``visibility=student_ok`` (and overrides any explicit ``visibility`` arg).
    Teachers/admins pass ``role='teacher'`` or omit it to see everything.

    Any clause may be a single string (matched with ``$eq``) or a list
    (matched with ``$in``).
    """

    # Resolve the visibility constraint from role/visibility args.
    effective_visibility: str | Sequence[str] | None = visibility
    if role:
        normalized_role = role.strip().lower()
        if normalized_role in STUDENT_ROLES:
            effective_visibility = "student_ok"
        elif normalized_role in TEACHER_ROLES:
            # No visibility filter — teacher sees both.
            effective_visibility = None
        else:
            raise ValueError(f"Unknown role '{role}'. Expected one of student/teacher/admin.")

    clauses: list[dict[str, Any]] = []

    if subject:
        normalized_subject = subject.strip().lower()
        if normalized_subject not in SUPPORTED_SUBJECTS:
            raise ValueError(
                f"Unsupported subject '{subject}'. Expected one of: {sorted(SUPPORTED_SUBJECTS)}."
            )
        clauses.append({"subject": {"$eq": normalized_subject}})

    for key, value in (
        ("chapter", chapter),
        ("section", section),
        ("topic", topic),
        ("level", level),
        ("board", board),
        ("content_type", content_type),
        ("document_id", document_id),
    ):
        if value:
            clauses.append({key: {"$eq": str(value)}})

    if chunk_type:
        normalized = _normalize_set(chunk_type, valid=VALID_CHUNK_TYPES, field="chunk_type")
        clauses.append(_in_or_eq("chunk_type", normalized))

    if effective_visibility:
        normalized = _normalize_set(effective_visibility, valid=VALID_VISIBILITY, field="visibility")
        clauses.append(_in_or_eq("visibility", normalized))

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _normalize_set(value: str | Sequence[str], *, valid: set[str], field: str) -> list[str]:
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value)
    normalized = [v.strip().lower() for v in values if v and v.strip()]
    for v in normalized:
        if v not in valid:
            raise ValueError(f"Unsupported {field} value '{v}'. Expected one of: {sorted(valid)}.")
    return normalized


def _in_or_eq(field: str, values: list[str]) -> dict[str, Any]:
    if len(values) == 1:
        return {field: {"$eq": values[0]}}
    return {field: {"$in": values}}
