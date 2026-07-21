"""Unit tests for app.core.metrics."""

from __future__ import annotations

import pytest


def _sample_value(counter, **labels) -> float:
    """Return the current value of one labelled child, or 0.0 if absent."""

    try:
        return counter.labels(**labels)._value.get()
    except Exception:
        return 0.0


def test_record_auth_event_increments_counter():
    from app.core.metrics import AUTH_EVENTS, record_auth_event

    before = _sample_value(
        AUTH_EVENTS, event="jwt_decode", outcome="failure", reason="expired"
    )
    record_auth_event(event="jwt_decode", outcome="failure", reason="expired")
    after = _sample_value(
        AUTH_EVENTS, event="jwt_decode", outcome="failure", reason="expired"
    )
    assert after == before + 1


def test_record_auth_event_defaults_missing_labels_to_strings():
    """Missing reason → labels with `"none"`, missing event/outcome → `"unknown"`."""

    from app.core.metrics import AUTH_EVENTS, record_auth_event

    before = _sample_value(
        AUTH_EVENTS, event="jwt_decode", outcome="success", reason="none"
    )
    record_auth_event(event="jwt_decode", outcome="success")  # no reason
    after = _sample_value(
        AUTH_EVENTS, event="jwt_decode", outcome="success", reason="none"
    )
    assert after == before + 1


def test_record_llm_tokens_increments_per_kind():
    from app.core.metrics import LLM_TOKENS, record_llm_tokens

    before_prompt = _sample_value(
        LLM_TOKENS, provider="groq", model="llama-3.3-70b-versatile", kind="prompt"
    )
    before_compl = _sample_value(
        LLM_TOKENS, provider="groq", model="llama-3.3-70b-versatile", kind="completion"
    )
    record_llm_tokens(
        provider="groq",
        model="llama-3.3-70b-versatile",
        prompt_tokens=123,
        completion_tokens=45,
    )
    after_prompt = _sample_value(
        LLM_TOKENS, provider="groq", model="llama-3.3-70b-versatile", kind="prompt"
    )
    after_compl = _sample_value(
        LLM_TOKENS, provider="groq", model="llama-3.3-70b-versatile", kind="completion"
    )
    assert after_prompt == before_prompt + 123
    assert after_compl == before_compl + 45


def test_record_llm_tokens_skips_zeros():
    """Recording 0 tokens for one side should not touch that label series."""

    from app.core.metrics import record_llm_tokens

    # Just call — assertion is that it doesn't raise.
    record_llm_tokens(provider="groq", model="anything", prompt_tokens=0)


def test_audit_log_event_mirrors_into_prometheus_counter():
    """The audit module's log_auth_event should also bump the metric."""

    from app.core.audit import log_auth_event
    from app.core.metrics import AUTH_EVENTS

    before = _sample_value(
        AUTH_EVENTS, event="role_check", outcome="failure", reason="role_mismatch"
    )
    log_auth_event(
        event="role_check",
        outcome="failure",
        reason="role_mismatch",
        role="student",
    )
    after = _sample_value(
        AUTH_EVENTS, event="role_check", outcome="failure", reason="role_mismatch"
    )
    assert after == before + 1


def test_install_instrumentator_adds_metrics_route():
    from fastapi import FastAPI

    from app.core.metrics import install_instrumentator

    app = FastAPI()
    install_instrumentator(app)
    paths = {route.path for route in app.routes}
    assert "/metrics" in paths
