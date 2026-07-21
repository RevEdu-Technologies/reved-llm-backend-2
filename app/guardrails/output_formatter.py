"""Final output formatting helpers for student-facing grounded answers."""

from __future__ import annotations

import re

# Matches lines that start with a number followed by a dot/parenthesis and a space,
# e.g. "1. ", "2) ", which indicates a numbered list item.
_NUMBERED_LINE = re.compile(r"^\d+[.)]\s+", re.MULTILINE)

# Matches bold section labels the model sometimes produces despite instructions,
# e.g. "**Direct Answer:**", "**Explanation:**"
_SECTION_LABEL = re.compile(
    r"^\*{0,2}(Direct Answer|Explanation|Example|Analogy|Quick Check|Reinforcement)"
    r"[:\s]*\*{0,2}[:\s]*",
    re.IGNORECASE | re.MULTILINE,
)


def strip_numbered_formatting(answer: str) -> str:
    """Remove numbered-list prefixes and section labels from the answer.

    This acts as a safety net: if the model still produces numbered output
    despite the updated prompt instructions, this function cleans it up so
    the student always receives natural-prose formatting.
    """

    cleaned = _NUMBERED_LINE.sub("", answer)
    cleaned = _SECTION_LABEL.sub("", cleaned)
    # Collapse any triple-or-more newlines left by removals.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def ensure_teacherly_close(answer: str) -> str:
    """Make sure the answer ends cleanly for terminal and later API use."""

    answer = strip_numbered_formatting(answer)
    return answer.strip()
