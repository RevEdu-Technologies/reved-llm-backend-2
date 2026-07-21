"""Heuristic chunk-type and quality classifier.

Used during chunking to:
  * Tag each chunk with a ``chunk_type`` (definition/formula/worked_example/
    exercise/solution/figure/summary/misc).
  * Decide ``visibility`` (student_ok vs teacher_only).
  * Compute a quality score; very-low-quality chunks are dropped.

The classifier is pattern-based and deliberately simple. Step 3 adds an LLM
fallback (Groq) for chunks the heuristic tags ``misc``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- Patterns --------------------------------------------------------------

# A definition typically has language asserting "X is Y" or "X means Y".
_DEFINITION_RE = re.compile(
    r"\b(is defined as|are defined as|refers? to|is the term for|means that|"
    r"is called|are called|is known as|definition[:\s])",
    re.IGNORECASE,
)
_DEFINITION_START = re.compile(r"^\s*(Definition|Definitions)\b", re.IGNORECASE)

# A worked example has explicit "Example" framing and usually a "Solution" block.
_EXAMPLE_START = re.compile(
    r"^\s*(Example|Worked Example|Sample Problem|Illustration)\s*\d*[:\.]?",
    re.IGNORECASE,
)
_SOLUTION_MARKER = re.compile(r"\b(Solution|Answer)\s*[:\.]", re.IGNORECASE)

# An exercise prompts the reader to compute/derive/show.
_EXERCISE_START = re.compile(
    r"^\s*(Exercise|Question|Problem|Q\d+|Try this|Practice)\b",
    re.IGNORECASE,
)
_EXERCISE_VERB = re.compile(
    r"\b(calculate|find|determine|compute|derive|show that|prove|estimate|"
    r"evaluate|state|explain why|how many|how much)\b",
    re.IGNORECASE,
)
_QUESTION_MARK_END = re.compile(r"\?\s*$")

# Solutions (without an enclosing example block) and marking schemes.
_SOLUTION_START = re.compile(
    r"^\s*(Solution|Answer|Marking Scheme|Marking Guide|Rubric)\b[:\.]?",
    re.IGNORECASE,
)
_MARKING_HINT = re.compile(
    r"\b(marking guide|marking scheme|rubric|grading criteria)\b",
    re.IGNORECASE,
)

# Figure / caption (short, descriptive, often near images in the source).
_FIGURE_START = re.compile(
    r"^\s*(Figure|Fig\.?|Diagram|Plate|Chart|Graph|Table)\s*\d", re.IGNORECASE
)

# Summary / review / key-points blocks.
_SUMMARY_START = re.compile(
    r"^\s*(Summary|Chapter Summary|Section Summary|Key Points|Key Equations|"
    r"Key Terms|Review|Chapter Review|Test Prep)\b",
    re.IGNORECASE,
)

# Formula detection — count `=` operators and check char composition.
_EQUALS_TOKEN = re.compile(r"(?<![<>=!])=(?!=)")  # exclude ==, !=, <=, >=
_MATH_SYMBOLS = set("=+-*/^√∑∫πθαβγδλμωΩΔ∂∇±≈≠≤≥<>{}()[]|·×÷")


# --- Visibility model ------------------------------------------------------
#
# Visibility is a property of the SOURCE, not the chunk content. A textbook
# worked-solution is `student_ok` because it lives in a textbook (a pedagogical
# resource); a marking guide is `teacher_only` because it lives in a teacher
# guide folder, even if a particular chunk reads like a textbook paragraph.
# This keeps the visibility decision auditable (it's a property of where the
# file came from, not a brittle content-pattern guess).

CONTENT_TYPE_VISIBILITY: dict[str, str] = {
    # Student-accessible sources
    "textbook":        "student_ok",
    "syllabus":        "student_ok",
    "past_question":   "student_ok",
    "curriculum":      "student_ok",
    # Teacher-exclusive sources
    "teacher_guide":   "teacher_only",
    "scheme_of_work":  "teacher_only",
    "marking_guide":   "teacher_only",
    "lesson_note":     "teacher_only",
}


def visibility_for_content_type(content_type: str) -> str:
    """Resolve the default visibility for a source content_type.

    Defaults to ``student_ok`` for unknown types so a missing entry doesn't
    accidentally cause student-blocking; if you intend something to be
    teacher-only, register it explicitly in CONTENT_TYPE_VISIBILITY.
    """
    return CONTENT_TYPE_VISIBILITY.get(content_type, "student_ok")


# --- Public API ------------------------------------------------------------


@dataclass(slots=True)
class ChunkClassification:
    """Output of the chunk classifier.

    NOTE: visibility is intentionally NOT on this object. It is set at the
    source/content_type level, not derived from chunk content.
    """

    chunk_type: str
    quality_score: float  # 0.0 (drop) to 1.0 (keep)
    quality_reason: str
    tags: list[str]


def classify_chunk(text: str) -> ChunkClassification:
    """Run heuristics to tag a chunk's type, quality, and discovery tags."""

    stripped = (text or "").strip()
    tags: list[str] = []
    chunk_type = _detect_chunk_type(stripped, tags)

    # Marking-related language is preserved as a TAG only — useful as a
    # retrieval hint, but it does NOT flip visibility. Source-level mapping
    # is authoritative.
    if _MARKING_HINT.search(stripped):
        tags.append("contains_marking_guide")

    quality_score, quality_reason = _score_quality(stripped)

    return ChunkClassification(
        chunk_type=chunk_type,
        quality_score=quality_score,
        quality_reason=quality_reason,
        tags=tags,
    )


# --- Type detection --------------------------------------------------------


def _line_starts_match(pattern: re.Pattern[str], text: str) -> bool:
    """True if any non-blank line in the text starts with ``pattern``."""
    return any(pattern.match(line) for line in text.splitlines() if line.strip())


def _detect_chunk_type(text: str, tags: list[str]) -> str:
    if not text:
        return "misc"

    first_line = text.splitlines()[0]

    # Solutions / marking schemes — high precision, check first. A chunk that
    # is dominated by a Solution block is teacher-only regardless of context.
    if _SOLUTION_START.match(first_line):
        return "solution"

    # Worked example: any line in the chunk starts with "Example".
    # (Our chunks often absorb the preceding section heading, so the example
    # marker isn't always on the first line.)
    if _line_starts_match(_EXAMPLE_START, text):
        if _SOLUTION_MARKER.search(text):
            tags.append("with_solution_inline")
        return "worked_example"

    # A standalone Solution block without an enclosing Example is still
    # teacher-only material.
    if _line_starts_match(_SOLUTION_START, text):
        return "solution"

    if _line_starts_match(_FIGURE_START, text) and len(text) < 600:
        return "figure"

    if _line_starts_match(_SUMMARY_START, text):
        return "summary"

    # Exercise: explicit "Exercise N" / "Question N" line OR a question-mark
    # ending paired with an imperative verb.
    if _line_starts_match(_EXERCISE_START, text):
        return "exercise"
    if _QUESTION_MARK_END.search(text) and _EXERCISE_VERB.search(text):
        return "exercise"

    if _DEFINITION_START.match(first_line) or _DEFINITION_RE.search(text):
        return "definition"

    if _is_formula_dense(text):
        return "formula"

    # Default to "explanation". Most textbook body text that didn't match a
    # more specific pattern (definition / example / exercise / etc.) IS
    # explanatory prose. "misc" is reserved for content the LLM fallback
    # can't classify or that genuinely doesn't fit any category — see
    # llm_classifier.py for the recovery path when used.
    return "explanation"


def _is_formula_dense(text: str) -> bool:
    """Treat the chunk as a formula block if it has multiple ``=`` and a high
    ratio of math symbols. Pure ``=`` lines beat narrative prose by far."""
    equals_count = len(_EQUALS_TOKEN.findall(text))
    if equals_count < 2:
        return False
    if not text:
        return False
    math_char_count = sum(1 for ch in text if ch in _MATH_SYMBOLS)
    alpha_count = sum(1 for ch in text if ch.isalpha())
    if alpha_count == 0:
        return True
    return (math_char_count / max(alpha_count, 1)) >= 0.20


# --- Quality scoring -------------------------------------------------------

_BOILERPLATE_LINE = re.compile(
    r"^\s*(see (page|chapter|section|figure)|continued on page|"
    r"www\.|http[s]?://|©|all rights reserved)",
    re.IGNORECASE,
)


def _score_quality(text: str) -> tuple[float, str]:
    """Return (score, reason). Scores below 0.25 are dropped at write time."""
    if not text:
        return 0.0, "empty"

    if len(text) < 80:
        return 0.0, "too_short"

    # Ratio of letters to total non-space chars — flags TOC/index/math-only chunks.
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0, "no_visible_chars"
    alpha = sum(1 for c in visible if c.isalpha())
    alpha_ratio = alpha / len(visible)
    if alpha_ratio < 0.35:
        return 0.0, f"low_alpha_ratio={alpha_ratio:.2f}"

    # Boilerplate-dominated chunks.
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and sum(1 for line in lines if _BOILERPLATE_LINE.match(line)) >= max(2, len(lines) // 2):
        return 0.1, "boilerplate_dominated"

    # Otherwise good — score scales with alpha ratio.
    return min(1.0, alpha_ratio * 1.2), "ok"


# Threshold used by the chunker to drop low-quality records before write.
QUALITY_DROP_THRESHOLD = 0.25
