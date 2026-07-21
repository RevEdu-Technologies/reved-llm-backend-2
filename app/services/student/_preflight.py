"""Three-tier preflight for student questions.

Pipeline:
    1. Deterministic subject fuzzy match (free, instant).
    2. LLM preflight for spelling/grammar + ambiguity detection (single
       cheap Groq call using the preflight model).
    3. Safety-net clarifier if tier 1 + 2 both fail.

Output is a ``PreflightResult`` consumed by the tutor service. The service
decides whether to answer or to return the clarifier back to the student.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from app.core.config import Settings
from app.llm.client import GroqChatClient, LLMClientError
from app.prompts.student import PREFLIGHT_SYSTEM_PROMPT, build_preflight_prompt
from app.services.student._llm_json import parse_json_response
from app.services.student._subject_matcher import (
    CANONICAL_SUBJECTS,
    CanonicalSubject,
    normalize_subject,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreflightResult:
    """Normalized question + clarity verdict, ready for the tutor."""

    original_question: str
    corrected_question: str
    original_subject: str | None
    corrected_subject: CanonicalSubject | None
    needs_clarification: bool
    clarifying_question: str | None
    # Provenance flags so the service can surface "Did you mean …?" hints.
    question_was_corrected: bool
    subject_was_corrected: bool
    subject_source: Literal["exact", "fuzzy", "llm", "unresolved"]
    # Educational-intent judgment from the LLM preflight. ``None`` when the
    # preflight call failed — the tutor service falls back to the hardcoded
    # role validator in that case.
    is_educational: bool | None
    off_topic_reason: str | None


class StudentPreflight:
    """Three-tier preflight runner."""

    def __init__(
        self,
        *,
        llm_client: GroqChatClient | None,
        preflight_model: str,
        preflight_max_tokens: int,
    ) -> None:
        self._llm_client = llm_client
        self._preflight_model = preflight_model
        self._preflight_max_tokens = preflight_max_tokens

    @classmethod
    def from_settings(cls, settings: Settings) -> "StudentPreflight":
        return cls(
            llm_client=GroqChatClient.from_settings(settings),
            preflight_model=settings.groq_preflight_model,
            preflight_max_tokens=settings.groq_preflight_max_tokens,
        )

    async def run(
        self,
        *,
        question: str,
        subject_hint: str | None,
        student_class: str,
        history: Sequence[Any] | None = None,
    ) -> PreflightResult:
        """Normalize a question and judge whether it's ready to answer."""

        original_question = question
        original_subject = subject_hint

        # Tier 1: deterministic subject match.
        t1_subject, t1_confidence = normalize_subject(subject_hint)
        subject_source: Literal["exact", "fuzzy", "llm", "unresolved"] = (
            "exact" if t1_confidence >= 0.999 else "fuzzy" if t1_subject else "unresolved"
        )

        # Tier 2: LLM preflight. Cleans up the question; also gives us a
        # second opinion on the subject when tier 1 couldn't resolve it.
        tier2 = await self._run_llm_preflight(
            question=question,
            subject_hint=subject_hint,
            student_class=student_class,
            history=history,
        )

        corrected_question = (tier2.get("corrected_question") or question).strip()
        if not corrected_question:
            corrected_question = question

        llm_subject = _parse_subject(tier2.get("corrected_subject"))
        corrected_subject: CanonicalSubject | None = t1_subject
        if corrected_subject is None and llm_subject is not None:
            corrected_subject = llm_subject
            subject_source = "llm"

        needs_clarification = bool(tier2.get("needs_clarification"))
        clarifying_question = _clean_optional_str(tier2.get("clarifying_question"))

        llm_call_succeeded = bool(tier2)
        if llm_call_succeeded:
            is_educational: bool | None = bool(tier2.get("is_educational", True))
        else:
            is_educational = None
        off_topic_reason = _clean_optional_str(tier2.get("off_topic_reason"))

        # Safety overrides for is_educational=False. The 8B preflight model
        # is over-eager to mark short subject-tagged follow-ups off-topic.
        # The role_validator's red-flag list still runs downstream, so truly
        # disallowed asks are caught regardless of what we set here.
        if is_educational is False:
            if _prior_assistant_was_clarifier(history):
                logger.info(
                    "Preflight: overriding is_educational=false because prior "
                    "assistant turn was a clarifier."
                )
                is_educational = True
                off_topic_reason = None
            elif corrected_subject is not None:
                logger.info(
                    "Preflight: overriding is_educational=false because the "
                    "input resolved to canonical subject '%s'.",
                    corrected_subject,
                )
                is_educational = True
                off_topic_reason = None

        # Tier 3: safety-net clarifier. If after tiers 1 + 2 we still have no
        # subject AND the question plausibly needs one, raise a clarifier.
        if (
            corrected_subject is None
            and original_subject
            and original_subject.strip()
            and not needs_clarification
        ):
            needs_clarification = True
            clarifying_question = (
                f"I wasn't sure which subject you meant by '{original_subject.strip()}'. "
                "Could you say it again — for example: mathematics, english language, "
                "physics, chemistry, biology, economics, government, history, "
                "commerce, accounting, computer studies, or religious studies?"
            )

        return PreflightResult(
            original_question=original_question,
            corrected_question=corrected_question,
            original_subject=original_subject,
            corrected_subject=corrected_subject,
            needs_clarification=needs_clarification,
            clarifying_question=clarifying_question if needs_clarification else None,
            question_was_corrected=_differs(original_question, corrected_question),
            subject_was_corrected=(
                original_subject is not None
                and corrected_subject is not None
                and (original_subject.strip().lower() != corrected_subject)
            ),
            subject_source=subject_source,
            is_educational=is_educational,
            off_topic_reason=off_topic_reason if is_educational is False else None,
        )

    async def _run_llm_preflight(
        self,
        *,
        question: str,
        subject_hint: str | None,
        student_class: str,
        history: Sequence[Any] | None = None,
    ) -> dict:
        """Return the parsed JSON dict from the preflight LLM call.

        Failures are logged and downgraded to an empty dict so the tutor
        still responds — subject stays whatever tier 1 produced, and the
        corrected question defaults to the original.
        """

        if self._llm_client is None:
            return {}

        prompt = build_preflight_prompt(
            question=question,
            subject_hint=subject_hint,
            student_class=student_class,
            history=history,
        )
        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=PREFLIGHT_SYSTEM_PROMPT,
                user_prompt=prompt,
                model=self._preflight_model,
                temperature=0.0,
                max_completion_tokens=self._preflight_max_tokens,
                response_format={"type": "json_object"},
            )
        except LLMClientError as exc:
            logger.warning("Preflight LLM failed: %s", exc)
            return {}

        try:
            return parse_json_response(response.text)
        except Exception as exc:  # noqa: BLE001 - preflight is best-effort
            logger.warning("Preflight JSON parse failed: %s", exc)
            return {}


def _parse_subject(raw: object) -> CanonicalSubject | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized in CANONICAL_SUBJECTS:
        return normalized  # type: ignore[return-value]
    return None


def _clean_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _differs(a: str, b: str) -> bool:
    return a.strip() != b.strip()


# Signals that an assistant turn is a clarifying question rather than a final
# answer. Mirrors the heuristic in app.guardrails.role_validator and is used
# here to detect when the user's reply is a follow-up to a clarifier.
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


def _prior_assistant_was_clarifier(history: Sequence[Any] | None) -> bool:
    """True if the most recent assistant turn in history reads as a clarifier."""
    if not history:
        return False
    for turn in reversed(list(history)):
        role = getattr(turn, "role", None)
        content = getattr(turn, "content", None)
        if role is None and isinstance(turn, dict):
            role = turn.get("role")
            content = turn.get("content")
        role_str = str(role or "").lower()
        content_str = str(content or "")
        if not role_str or not content_str:
            continue
        if role_str != "assistant":
            return False
        lowered = content_str.lower()
        return any(signal in lowered for signal in _CLARIFIER_SIGNALS)
    return False


__all__ = ["PreflightResult", "StudentPreflight"]
