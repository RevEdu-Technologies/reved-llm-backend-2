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


class TestFromEnvMissingDatabaseUrl:
    """DATABASE_URL absence must warn, not crash ``Settings.from_env()``.

    Regression coverage for the Render outage where a missing DATABASE_URL
    raised ConfigurationError at import time, so the process never bound a
    port and every route -- including the health checks -- 503'd behind the
    platform's edge proxy.
    """

    @pytest.fixture(autouse=True)
    def _minimal_valid_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Everything from_env() needs *besides* DATABASE_URL.
        monkeypatch.setenv("GROQ_API_KEY", "groq-test")
        monkeypatch.setenv("PINECONE_API_KEY", "pc-test")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        # Never read a real .env file into the test process.
        monkeypatch.setattr(
            "app.core.config._load_dotenv_if_available", lambda: None
        )

    def test_missing_database_url_does_not_raise(self) -> None:
        settings = Settings.from_env()  # must not raise
        assert settings.database_url == ""
        assert settings.database_sync_url == ""

    def test_missing_database_url_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="app.core.config"):
            Settings.from_env()
        assert any("DATABASE_URL is not set" in r.message for r in caplog.records)

    def test_present_database_url_is_normalized_for_async(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://u:p@h:5432/db"
        )
        settings = Settings.from_env()
        assert settings.database_url == "postgresql+asyncpg://u:p@h:5432/db"
        assert settings.database_sync_url == "postgresql+psycopg://u:p@h:5432/db"
