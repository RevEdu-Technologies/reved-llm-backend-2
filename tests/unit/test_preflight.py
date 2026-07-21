"""Unit tests for the three-tier student preflight."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.services.student._preflight import PreflightResult, StudentPreflight


class FakeLLMClient:
    """Stub LLM client that returns a canned JSON payload."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def generate(
        self,
        *,
        system_prompt,
        user_prompt,
        model=None,
        temperature=None,
        max_completion_tokens=None,
        response_format=None,
    ):
        self.calls.append({"model": model, "response_format": response_format})
        return type("R", (), {"text": json.dumps(self._payload)})


def _make_preflight(payload: dict) -> tuple[StudentPreflight, FakeLLMClient]:
    client = FakeLLMClient(payload)
    service = StudentPreflight(
        llm_client=client,
        preflight_model="fake-small",
        preflight_max_tokens=200,
    )
    return service, client


def _run(coro):
    return asyncio.run(coro)


def test_tier1_exact_subject_bypasses_llm_correction():
    service, _ = _make_preflight(
        {
            "corrected_question": "What is photosynthesis?",
            "corrected_subject": "biology",
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    result = _run(
        service.run(
            question="What is photosynthesis?",
            subject_hint="biology",
            student_class="Primary 5",
        )
    )
    assert isinstance(result, PreflightResult)
    assert result.corrected_subject == "biology"
    assert result.subject_source == "exact"
    assert result.needs_clarification is False


def test_tier1_fuzzy_fixes_typo_subject():
    service, _ = _make_preflight(
        {
            "corrected_question": "What is an acid?",
            "corrected_subject": "chemistry",
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    result = _run(
        service.run(
            question="What is an acid?",
            subject_hint="chemstry",
            student_class="SS1",
        )
    )
    assert result.corrected_subject == "chemistry"
    assert result.subject_was_corrected is True
    assert result.subject_source == "fuzzy"


def test_tier2_corrects_question_text():
    service, _ = _make_preflight(
        {
            "corrected_question": "What is photosynthesis?",
            "corrected_subject": "biology",
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    result = _run(
        service.run(
            question="wat is fotosynthesis",
            subject_hint="biology",
            student_class="JSS1",
        )
    )
    assert result.corrected_question == "What is photosynthesis?"
    assert result.question_was_corrected is True


def test_tier2_flags_ambiguous_question():
    service, _ = _make_preflight(
        {
            "corrected_question": "explain it",
            "corrected_subject": None,
            "needs_clarification": True,
            "clarifying_question": "What topic do you want me to explain?",
        }
    )
    result = _run(
        service.run(
            question="explain it",
            subject_hint=None,
            student_class="SS2",
        )
    )
    assert result.needs_clarification is True
    assert result.clarifying_question == "What topic do you want me to explain?"


def test_tier3_raises_clarifier_when_subject_unresolved():
    # ``subject_hint`` must be something tier 1 cannot resolve (not in the
    # canonical set, not in the alias map, not within the fuzzy threshold)
    # AND that tier 2's LLM mock also leaves unresolved
    # (``corrected_subject=None``). Tier 3 then synthesises the clarifier.
    # The original test used ``maths``; the alias map has since expanded
    # to map ``maths -> mathematics``, so tier 1 resolves it deterministically
    # and tier 3 never fires. ``basketball`` is a deliberate
    # not-a-school-subject that stays unresolved through both tiers.
    service, _ = _make_preflight(
        {
            "corrected_question": "What is gravity?",
            "corrected_subject": None,
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    result = _run(
        service.run(
            question="What is gravity?",
            subject_hint="basketball",
            student_class="JSS2",
        )
    )
    assert result.needs_clarification is True
    assert result.clarifying_question is not None
    assert "basketball" in result.clarifying_question


def test_no_subject_hint_does_not_force_clarifier():
    service, _ = _make_preflight(
        {
            "corrected_question": "What is gravity?",
            "corrected_subject": "physics",
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    result = _run(
        service.run(
            question="What is gravity?",
            subject_hint=None,
            student_class="JSS2",
        )
    )
    assert result.needs_clarification is False
    assert result.corrected_subject == "physics"
    assert result.subject_source == "llm"


def test_llm_client_none_degrades_gracefully():
    service = StudentPreflight(
        llm_client=None,
        preflight_model="fake-small",
        preflight_max_tokens=200,
    )
    result = _run(
        service.run(
            question="What is photosynthesis?",
            subject_hint="biology",
            student_class="Primary 5",
        )
    )
    assert result.corrected_subject == "biology"
    assert result.needs_clarification is False


def test_is_educational_true_when_llm_approves():
    service, _ = _make_preflight(
        {
            "corrected_question": "What is photosynthesis?",
            "corrected_subject": "biology",
            "needs_clarification": False,
            "clarifying_question": None,
            "is_educational": True,
            "off_topic_reason": "",
        }
    )
    result = _run(
        service.run(
            question="What is photosynthesis?",
            subject_hint="biology",
            student_class="Primary 5",
        )
    )
    assert result.is_educational is True
    assert result.off_topic_reason is None


def test_is_educational_false_surfaces_reason():
    service, _ = _make_preflight(
        {
            "corrected_question": "tell me gossip",
            "corrected_subject": None,
            "needs_clarification": False,
            "clarifying_question": None,
            "is_educational": False,
            "off_topic_reason": "That's not a learning question.",
        }
    )
    result = _run(
        service.run(
            question="tell me gossip",
            subject_hint=None,
            student_class="JSS1",
        )
    )
    assert result.is_educational is False
    assert result.off_topic_reason == "That's not a learning question."


def test_is_educational_none_when_llm_down():
    service = StudentPreflight(
        llm_client=None,
        preflight_model="fake-small",
        preflight_max_tokens=200,
    )
    result = _run(
        service.run(
            question="What is photosynthesis?",
            subject_hint="biology",
            student_class="Primary 5",
        )
    )
    assert result.is_educational is None


def test_llm_uses_json_object_response_format():
    service, client = _make_preflight(
        {
            "corrected_question": "What is photosynthesis?",
            "corrected_subject": "biology",
            "needs_clarification": False,
            "clarifying_question": None,
        }
    )
    _run(
        service.run(
            question="What is photosynthesis?",
            subject_hint="biology",
            student_class="Primary 5",
        )
    )
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["model"] == "fake-small"
