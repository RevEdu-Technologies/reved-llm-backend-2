"""Output filtering helpers for grounded student answers."""

from __future__ import annotations

import re

_BANNED_PATTERNS = [
    r"\baccording to the text\b",
    r"\baccording to the context\b",
    r"\baccording to the passage\b",
    r"\baccording to the material\b",
    r"\bas the text says\b",
    r"\bas the passage says\b",
    r"\bas the material says\b",
    r"\bas the lesson says\b",
    r"\bcontext says\b",
    r"\bsource says\b",
    r"\bsource \d+\b",
    r"\bdocument states\b",
    r"\bretrieved material\b",
    r"\bretrieved context\b",
    r"\bthe passage says\b",
    r"\bbased on the text\b",
    r"\bbased on the passage\b",
    r"\bbased on the material\b",
    r"\bfrom the textbook\b",
    r"\bfrom the material\b",
]

_SOURCE_LABELS = re.compile(r"\[Source\s+\d+\]", re.IGNORECASE)
_INTERNAL_DEBUG_PATTERNS = [
    r"\bsource\b",
    r"\bchunk\b",
    r"\bscore\b",
    r"\bretrieved\b",
    r"\bcontext\b",
]


def sanitize_student_answer(answer: str) -> str:
    """Remove obvious source/context leakage from the final answer."""

    cleaned = _SOURCE_LABELS.sub("", answer)
    for pattern in _BANNED_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def contains_forbidden_source_language(answer: str) -> bool:
    """Check whether the answer still exposes source/context language."""

    if _SOURCE_LABELS.search(answer):
        return True
    return any(re.search(pattern, answer, flags=re.IGNORECASE) for pattern in _BANNED_PATTERNS)


def contains_internal_debug_language(answer: str) -> bool:
    """Check whether the answer still exposes obvious debug or retrieval terms."""

    return any(re.search(pattern, answer, flags=re.IGNORECASE) for pattern in _INTERNAL_DEBUG_PATTERNS)
