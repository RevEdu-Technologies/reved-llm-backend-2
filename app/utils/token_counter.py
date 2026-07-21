"""Approximate token counting for LLM budgeting and usage tracking.

Exact token counts require a model-specific tokenizer, which is outside the
scope of this MVP. A conservative heuristic based on the 4-chars-per-token
rule used by most English-language Llama/Mixtral tokenizers is good enough to
keep prompts within model context limits and to log usage trends.
"""

from __future__ import annotations

_CHARS_PER_TOKEN: float = 4.0


def approximate_token_count(text: str) -> int:
    """Return a fast approximate token count for the given text.

    Parameters
    ----------
    text:
        Any string. Empty or whitespace-only inputs return 0.
    """

    if not text or not text.strip():
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def total_prompt_tokens(*parts: str) -> int:
    """Sum approximate tokens across several prompt fragments."""

    return sum(approximate_token_count(part) for part in parts)
