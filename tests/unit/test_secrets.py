"""Unit tests for app.core.secrets — the pluggable secrets loader."""

from __future__ import annotations

import pytest

from app.core import secrets as secrets_mod


@pytest.fixture(autouse=True)
def _isolate_secrets_state(monkeypatch):
    """Each test starts with a clean cache and no inherited backend env."""

    secrets_mod.clear_cache()
    for key in (
        "SECRETS_BACKEND",
        "SECRETS_PREFIX",
        "SECRETS_CACHE_TTL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    secrets_mod.clear_cache()


def test_env_backend_reads_from_fallback_env(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-live-abc")
    assert secrets_mod.load_secret("my_api_key", "MY_API_KEY") == "sk-live-abc"


def test_env_backend_defaults_to_uppercase_name(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-fallback")
    assert secrets_mod.load_secret("groq_api_key") == "sk-fallback"


def test_env_backend_returns_empty_when_missing(monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    assert secrets_mod.load_secret("does_not_exist") == ""


def test_env_backend_strips_whitespace(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "   sk-trimmed   ")
    assert secrets_mod.load_secret("my_api_key", "MY_API_KEY") == "sk-trimmed"


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "doesnotexist")
    with pytest.raises(RuntimeError, match="Unknown SECRETS_BACKEND"):
        secrets_mod.load_secret("anything", "ANYTHING")


def test_backend_value_wins_over_env(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("MY_API_KEY", "sk-from-env")
    monkeypatch.setattr(secrets_mod, "_read_aws", lambda secret_id: "sk-from-aws")
    assert secrets_mod.load_secret("my_api_key", "MY_API_KEY") == "sk-from-aws"


def test_backend_miss_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("MY_API_KEY", "sk-from-env")
    monkeypatch.setattr(secrets_mod, "_read_aws", lambda secret_id: None)
    assert secrets_mod.load_secret("my_api_key", "MY_API_KEY") == "sk-from-env"


def test_backend_exception_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("MY_API_KEY", "sk-from-env")

    def boom(secret_id):
        raise ValueError("boom")

    monkeypatch.setattr(secrets_mod, "_read_aws", boom)
    assert secrets_mod.load_secret("my_api_key", "MY_API_KEY") == "sk-from-env"


def test_backend_missing_dependency_propagates(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")

    def reader_raising_runtime(secret_id):
        raise RuntimeError("boto3 is required")

    monkeypatch.setattr(secrets_mod, "_read_aws", reader_raising_runtime)
    with pytest.raises(RuntimeError, match="boto3"):
        secrets_mod.load_secret("anything", "ANYTHING")


def test_prefix_is_applied_to_secret_id(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("SECRETS_PREFIX", "reved/prod")
    seen = {}

    def fake_reader(secret_id):
        seen["id"] = secret_id
        return "ok"

    monkeypatch.setattr(secrets_mod, "_read_aws", fake_reader)
    secrets_mod.load_secret("groq_api_key", "GROQ_API_KEY")
    assert seen["id"] == "reved/prod/groq_api_key"


def test_prefix_with_trailing_slash_is_handled(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("SECRETS_PREFIX", "reved/prod/")
    seen = {}

    def fake_reader(secret_id):
        seen["id"] = secret_id
        return "ok"

    monkeypatch.setattr(secrets_mod, "_read_aws", fake_reader)
    secrets_mod.load_secret("groq_api_key", "GROQ_API_KEY")
    assert seen["id"] == "reved/prod/groq_api_key"


def test_cache_hits_avoid_repeat_backend_reads(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    call_count = {"n": 0}

    def fake_reader(secret_id):
        call_count["n"] += 1
        return "sk-cached"

    monkeypatch.setattr(secrets_mod, "_read_aws", fake_reader)
    assert secrets_mod.load_secret("k", "K") == "sk-cached"
    assert secrets_mod.load_secret("k", "K") == "sk-cached"
    assert call_count["n"] == 1


def test_clear_cache_forces_reread(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    call_count = {"n": 0}

    def fake_reader(secret_id):
        call_count["n"] += 1
        return f"sk-{call_count['n']}"

    monkeypatch.setattr(secrets_mod, "_read_aws", fake_reader)
    assert secrets_mod.load_secret("k", "K") == "sk-1"
    secrets_mod.clear_cache()
    assert secrets_mod.load_secret("k", "K") == "sk-2"


def test_cache_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("SECRETS_CACHE_TTL_SECONDS", "0")
    call_count = {"n": 0}

    def fake_reader(secret_id):
        call_count["n"] += 1
        return f"sk-{call_count['n']}"

    monkeypatch.setattr(secrets_mod, "_read_aws", fake_reader)
    secrets_mod.load_secret("k", "K")
    secrets_mod.load_secret("k", "K")
    assert call_count["n"] == 2


def test_changing_backend_uses_separate_cache_entry(monkeypatch):
    """Cache key includes the backend so switching backends doesn't return stale values."""

    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setattr(secrets_mod, "_read_aws", lambda sid: "from-aws")
    assert secrets_mod.load_secret("k", "K") == "from-aws"

    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    monkeypatch.setattr(secrets_mod, "_read_vault", lambda sid: "from-vault")
    assert secrets_mod.load_secret("k", "K") == "from-vault"
