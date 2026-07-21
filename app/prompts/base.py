"""Shared prompt helpers and grounding rules for textbook QA."""

from __future__ import annotations

from pathlib import Path

GROUNDING_SYSTEM_PROMPT = """You are a careful learning assistant.
Use the background material only as hidden knowledge support.
The supporting information is internal and must never be named or referenced in the answer.
Never mention sources, citations, context, retrieval, documents, chunks, passages, or textbooks.
Never quote long passages verbatim or repeat the material word-for-word.
Answer like a teacher explaining a concept to a student.
If the material does not support the answer well enough, say you do not have enough information to explain it properly."""


def load_template(template_name: str) -> str:
    """Load a prompt template from the templates directory."""

    template_path = Path(__file__).resolve().parent / "templates" / template_name
    return template_path.read_text(encoding="utf-8")
