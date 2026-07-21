"""Role boundary enforcement for RevEd queries and outputs.

Each user role in RevEd has a distinct scope of allowed AI assistance. This
module rejects cross-role requests (e.g. a student asking for teacher-only
output like full lesson plans, a parent asking for admin analytics) before
they reach the LLM. It runs purely on the request payload and never touches
the vector store or the LLM.

It also provides an output-side check used by services to confirm the LLM
didn't drift into another role's voice or persona.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

Role = Literal["student", "teacher", "parent", "admin"]

_EDUCATIONAL_DOMAIN_HINTS = (
    # Core subjects
    "math", "maths", "mathematics", "arithmetic", "algebra", "geometry",
    "trigonometry", "calculus", "statistics",
    "science", "biology", "bio", "chemistry", "chem", "physics", "phys",
    "english", "literature", "grammar", "comprehension", "essay",
    "history", "geography", "economics", "commerce", "accounting",
    "civics", "government", "social studies", "citizenship",
    "agriculture", "agric", "home economics", "food and nutrition",
    "religious studies", "islamic", "christian",
    "computer", "computing", "ict", "coding", "programming",
    "further mathematics", "technical drawing", "fine art", "music",
    "french", "yoruba", "igbo", "hausa", "swahili",
    # Learning verbs / intents
    "learn", "learning", "study", "studying", "revise", "revision",
    "understand", "understanding", "explain", "explanation",
    "teach", "tutor", "practice", "practise", "master",
    "define", "definition", "describe", "summarize", "summarise",
    "compare", "contrast", "analyse", "analyze", "prove", "derive",
    "solve", "calculate", "compute", "simplify", "factor", "factorise",
    "find the", "work out", "show me", "help me",
    # School / exam context
    "homework", "assignment", "exam", "test", "quiz", "mock",
    "waec", "neco", "jamb", "utme", "bece", "ijmb",
    "topic", "subtopic", "concept", "chapter", "unit", "lesson",
    "school", "class", "grade", "level", "syllabus", "curriculum",
    "note", "notes", "textbook", "past question", "past paper",
    # Goals & career
    "goal", "career", "subject", "course",
    # Common question tail
    "example", "examples", "worked example", "step by step",
)

_NON_EDUCATIONAL_RED_FLAGS = (
    r"\bhow do i (hack|crack|cheat)\b",
    r"\bbuy\s+\w+\s+online\b",
    r"\badult\s+content\b",
    r"\bpolitical\s+campaign\b",
    r"\bstock tips?\b",
    r"\bgambl(e|ing)\b",
)

_STUDENT_FORBIDDEN_ASKS = (
    r"\bgenerate a (full|complete) lesson plan\b",
    r"\bbuild a quiz for my class\b",
    r"\bschool[- ]wide analytics\b",
    r"\bapprove (this|the) resource\b",
    r"\bcompliance report\b",
)

_TEACHER_FORBIDDEN_ASKS = (
    r"\bparent(ing)? advice for my child\b",
    r"\bschool[- ]wide analytics\b",
    r"\bapprove (this|the) resource\b",
)

_PARENT_FORBIDDEN_ASKS = (
    r"\bsolve this homework\b",
    r"\bwrite a lesson plan\b",
    r"\bschool[- ]wide analytics\b",
    r"\bcompliance report\b",
)


@dataclass(slots=True)
class RoleValidationResult:
    """Outcome of a role-scope validation."""

    allowed: bool
    reason: str | None = None


def validate_query_for_role(
    query: str,
    role: Role,
    *,
    history: Sequence[object] | None = None,
    skip_educational_check: bool = False,
) -> RoleValidationResult:
    """Ensure a user query belongs to the caller's role scope.

    Returns a result object rather than raising, so callers can choose whether
    to refuse the request or return a gentle redirect message.

    ``history`` (optional) is a sequence of prior conversation turns. Each
    entry is expected to expose ``role`` and ``content`` attributes (or keys).
    When supplied, it is used two ways:

      1. If the previous assistant turn looks like a clarifier asked by the
         tutor, the "looks educational" check is skipped for the current
         reply — the student is just completing an existing educational
         thread, so fragments like ``"photosynthesis"`` are allowed through.
      2. Otherwise, the educational-intent check runs over the joined
         conversation text (all user turns + current query), giving the
         filter context instead of judging a single fragment in isolation.

    The red-flag and forbidden-pattern checks always run on the current
    query only — history never unlocks disallowed intent.
    """

    normalized = (query or "").strip()
    if not normalized:
        return RoleValidationResult(
            allowed=False,
            reason="The question is empty.",
        )

    lowered = normalized.lower()

    if any(re.search(flag, lowered) for flag in _NON_EDUCATIONAL_RED_FLAGS):
        return RoleValidationResult(
            allowed=False,
            reason="This assistant only helps with educational topics.",
        )

    if not skip_educational_check and not _prior_assistant_was_clarifier(history):
        joined = _joined_conversation_text(history, lowered)
        if not _looks_educational(joined):
            return RoleValidationResult(
                allowed=False,
                reason="This assistant only helps with educational topics.",
            )

    forbidden_patterns = _forbidden_patterns_for(role)
    for pattern in forbidden_patterns:
        if re.search(pattern, lowered):
            return RoleValidationResult(
                allowed=False,
                reason=f"That request is outside the {role} experience.",
            )

    return RoleValidationResult(allowed=True)


def _looks_educational(lowered_text: str) -> bool:
    return (
        any(hint in lowered_text for hint in _EDUCATIONAL_DOMAIN_HINTS)
        or _contains_question_signal(lowered_text)
    )


def _contains_question_signal(lowered_text: str) -> bool:
    question_words = (
        "what", "why", "how", "when", "where", "which", "who",
        "explain", "define", "describe", "solve", "calculate",
        "prove", "derive", "simplify", "find",
    )
    if "?" in lowered_text:
        return True
    return any(
        lowered_text.startswith(word + " ") or f" {word} " in lowered_text
        for word in question_words
    )


_CLARIFIER_SIGNALS = (
    "?",
    "what do you mean",
    "which",
    "could you clarify",
    "can you clarify",
    "what topic",
    "what subject",
    "i wasn't sure",
    "i was not sure",
)


def _prior_assistant_was_clarifier(history: Sequence[object] | None) -> bool:
    last = _last_turn(history)
    if last is None:
        return False
    turn_role, content = last
    if turn_role != "assistant":
        return False
    lowered = content.lower()
    return any(signal in lowered for signal in _CLARIFIER_SIGNALS)


def _joined_conversation_text(
    history: Sequence[object] | None, current_lowered: str
) -> str:
    if not history:
        return current_lowered
    parts: list[str] = []
    for turn in history:
        turn_role, content = _extract_turn(turn)
        if turn_role == "user" and content:
            parts.append(content.lower())
    parts.append(current_lowered)
    return " \n ".join(parts)


def _last_turn(history: Sequence[object] | None) -> tuple[str, str] | None:
    if not history:
        return None
    for turn in reversed(list(history)):
        extracted = _extract_turn(turn)
        if extracted[0] and extracted[1]:
            return extracted
    return None


def _extract_turn(turn: object) -> tuple[str, str]:
    role = getattr(turn, "role", None)
    content = getattr(turn, "content", None)
    if role is None and isinstance(turn, dict):
        role = turn.get("role")
        content = turn.get("content")
    return (str(role or "").lower(), str(content or ""))


def _forbidden_patterns_for(role: Role) -> tuple[str, ...]:
    if role == "student":
        return _STUDENT_FORBIDDEN_ASKS
    if role == "teacher":
        return _TEACHER_FORBIDDEN_ASKS
    if role == "parent":
        return _PARENT_FORBIDDEN_ASKS
    return ()


_ROLE_VOICE_BREACH_PATTERNS = {
    "student": (
        r"\bas the admin\b",
        r"\bas the teacher\b",
        r"\bas the parent\b",
        r"\bhere is the compliance report\b",
        r"\bschool[- ]wide analytics\b",
    ),
    "teacher": (
        r"\bas the student\b",
        r"\bas the parent\b",
        r"\bas the admin\b",
    ),
    "parent": (
        r"\bas the student\b",
        r"\bas the admin\b",
    ),
    "admin": (
        r"\bas the student\b",
        r"\bas the parent\b",
    ),
}


def output_violates_role(answer: str, role: Role) -> bool:
    """Check whether the LLM output drifted out of the caller's role voice."""

    if not answer:
        return False
    patterns = _ROLE_VOICE_BREACH_PATTERNS.get(role, ())
    return any(re.search(pattern, answer, flags=re.IGNORECASE) for pattern in patterns)


__all__ = [
    "RoleValidationResult",
    "output_violates_role",
    "validate_query_for_role",
]
