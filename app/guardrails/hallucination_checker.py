"""Lightweight grounding checks for generated student answers."""

from __future__ import annotations

import re
from typing import Sequence

from app.rag.retrieval.retriever import RetrievalResult


def has_large_verbatim_overlap(
    answer: str,
    retrieval_results: Sequence[RetrievalResult],
    *,
    word_threshold: int = 25,
) -> bool:
    """Detect whether the answer copies a long exact phrase from retrieved chunks."""

    normalized_answer = _normalize(answer)
    if not normalized_answer:
        return False

    for result in retrieval_results:
        chunk_words = _normalize(result.text).split()
        if len(chunk_words) < word_threshold:
            continue
        for start in range(0, len(chunk_words) - word_threshold + 1):
            phrase = " ".join(chunk_words[start : start + word_threshold])
            if phrase and phrase in normalized_answer:
                return True
    return False


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()
