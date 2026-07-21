"""Tests for the production-mode safety checks in ``app.core.config``."""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings


def _make_settings(**overrides) -> Settings:
    """Build a Settings instance directly, bypassing env loading."""

    defaults: dict[str, object] = {
        "environment": "development",
        "cors_allowed_origins": ("http://localhost:3000",),
        "huggingface_api_key": "",
        "embedding_backend": "local",
        "hf_embedding_model": "BAAI/bge-base-en-v1.5",
        "hf_embedding_batch_size": 32,
        "hf_embedding_normalize": True,
        "embedding_query_prefix": "",
        "embedding_passage_prefix": "",
        "embedding_device": "cpu",
        "groq_api_key": "groq-test",
        "groq_model": "llama-3.3-70b-versatile",
        "groq_temperature": 0.1,
        "groq_max_completion_tokens": 700,
        "groq_preflight_model": "llama-3.3-70b-versatile",
        "groq_preflight_max_tokens": 500,
        "pinecone_api_key": "pc-test",
        "pinecone_index_name": "reved-index",
        "pinecone_dimension": 384,
        "pinecone_metric": "cosine",
        "pinecone_cloud": "aws",
        "pinecone_region": "us-east-1",
        "pinecone_namespace": "textbooks",
        "pinecone_upsert_batch_size": 100,
        "pinecone_include_chunk_text": False,
        "database_url": "postgresql+asyncpg://u:p@h:5432/db",
        "database_sync_url": "postgresql+psycopg://u:p@h:5432/db",
        "database_pool_size": 5,
        "database_max_overflow": 10,
        "supabase_url": "",
        "supabase_anon_key": "",
        "supabase_service_role_key": "",
        "supabase_jwt_secret": "",
        "supabase_jwt_algorithm": "HS256",
        "supabase_jwt_audience": "authenticated",
        "auth_enabled": False,
        "cache_backend": "memory",
        "cache_default_ttl_seconds": 300,
        "redis_url": None,
        "rate_limit_default_tier": "free",
        "rate_limit_llm_tiers": (),
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_development_allows_auth_disabled() -> None:
    settings = _make_settings(environment="development", auth_enabled=False)
    settings.validate()  # must not raise


@pytest.mark.parametrize("env_name", ["production", "prod", "staging", "Production", "STAGING"])
def test_production_like_env_rejects_auth_disabled(env_name: str) -> None:
    settings = _make_settings(environment=env_name, auth_enabled=False)
    with pytest.raises(ConfigurationError, match="AUTH_ENABLED=true"):
        settings.validate()


def test_production_with_auth_enabled_and_secret_validates() -> None:
    settings = _make_settings(
        environment="production",
        auth_enabled=True,
        supabase_jwt_secret="real-secret",
    )
    settings.validate()  # must not raise


def test_production_with_auth_enabled_but_missing_secret_rejected() -> None:
    settings = _make_settings(
        environment="production",
        auth_enabled=True,
        supabase_jwt_secret="",
    )
    with pytest.raises(ConfigurationError, match="SUPABASE_JWT_SECRET"):
        settings.validate()


def test_is_production_like_flag() -> None:
    assert _make_settings(environment="development").is_production_like is False
    assert _make_settings(environment="production").is_production_like is True
    assert _make_settings(environment="STAGING").is_production_like is True
    assert _make_settings(environment="prod").is_production_like is True
