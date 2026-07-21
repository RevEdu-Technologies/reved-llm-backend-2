"""Unit tests for the educational-intent / role-scope validator."""

from __future__ import annotations

from app.guardrails.role_validator import validate_query_for_role


def _turn(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class TestEducationalIntent:
    def test_plain_educational_question_is_allowed(self):
        result = validate_query_for_role("What is photosynthesis?", "student")
        assert result.allowed is True

    def test_non_educational_request_is_blocked(self):
        result = validate_query_for_role("tell me gossip about my friend", "student")
        assert result.allowed is False
        assert "educational" in (result.reason or "").lower()

    def test_expanded_list_accepts_waec_prep(self):
        result = validate_query_for_role("help me with my waec past question", "student")
        assert result.allowed is True

    def test_expanded_list_accepts_solve_verb(self):
        result = validate_query_for_role("solve 2x + 3 = 7", "student")
        assert result.allowed is True

    def test_expanded_list_accepts_local_language_subject(self):
        result = validate_query_for_role("explain this yoruba passage", "student")
        assert result.allowed is True


class TestHistoryAwareness:
    def test_fragment_reply_to_clarifier_is_allowed(self):
        """If the tutor just asked a clarifier, the next reply may be a
        single-word fragment that the keyword filter would otherwise block."""

        history = [
            _turn("user", "explain it"),
            _turn("assistant", "What do you mean by 'it'?"),
        ]
        result = validate_query_for_role(
            "photosynthesis", "student", history=history
        )
        assert result.allowed is True

    def test_joined_history_rescues_contextual_reply(self):
        """Even without a clarifier, if an earlier user turn made the intent
        clearly educational, a short follow-up should be allowed."""

        history = [
            _turn("user", "I'm studying biology for my waec exam"),
            _turn("assistant", "Great — which topic do you want to start with?"),
        ]
        result = validate_query_for_role(
            "cells", "student", history=history
        )
        assert result.allowed is True

    def test_history_does_not_bypass_red_flags(self):
        """Clarifier context should NOT allow disallowed intent through."""

        history = [
            _turn("user", "what is energy?"),
            _turn("assistant", "Which form of energy — kinetic or potential?"),
        ]
        result = validate_query_for_role(
            "how do i hack my teacher's laptop", "student", history=history
        )
        assert result.allowed is False

    def test_no_history_requires_educational_signal(self):
        result = validate_query_for_role("photosynthesis", "student")
        # single word, no "?", no question verb — but "photosynthesis" isn't
        # a keyword on its own either. Should be blocked without history.
        # (Biology is, so this also passes — adjust expectation.)
        # Actually "photosynthesis" has no match; let's use a neutral noun.
        result2 = validate_query_for_role("apples", "student")
        assert result2.allowed is False


class TestSkipEducationalCheck:
    def test_skip_flag_bypasses_educational_gate(self):
        """When the LLM preflight has already approved the intent, the
        hardcoded educational check can be skipped."""

        result = validate_query_for_role(
            "apples",
            "student",
            skip_educational_check=True,
        )
        assert result.allowed is True

    def test_skip_flag_still_blocks_forbidden_asks(self):
        """Role-scope forbidden patterns must always fire."""

        result = validate_query_for_role(
            "generate a full lesson plan",
            "student",
            skip_educational_check=True,
        )
        assert result.allowed is False

    def test_skip_flag_still_blocks_red_flags(self):
        result = validate_query_for_role(
            "how do i hack the portal",
            "student",
            skip_educational_check=True,
        )
        assert result.allowed is False
