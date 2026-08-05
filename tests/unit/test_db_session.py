"""Unit tests for app.db.session — engine creation when DATABASE_URL is unset.

Regression coverage for the outage where the whole app raised
ConfigurationError at import time when DATABASE_URL was missing, so Render
never bound a port and every route -- health checks included -- 503'd. Now
the app boots regardless (see tests/unit/test_config.py), and this module's
get_engine() is the single place a missing DATABASE_URL turns into a clean,
per-request 503 instead.
"""

from __future__ import annotations

import pytest

from app.core.config import ConfigurationError, Settings
from app.db import session as session_module


def _settings_without_database_url() -> Settings:
    return Settings(
        environment="development",
        cors_allowed_origins=("http://localhost:3000",),
        huggingface_api_key="",
        embedding_backend="local",
        hf_embedding_model="BAAI/bge-base-en-v1.5",
        hf_embedding_batch_size=32,
        hf_embedding_normalize=True,
        embedding_query_prefix="",
        embedding_passage_prefix="",
        embedding_device="cpu",
        groq_api_key="groq-test",
        groq_model="llama-3.3-70b-versatile",
        groq_temperature=0.1,
        groq_max_completion_tokens=700,
        groq_preflight_model="llama-3.3-70b-versatile",
        groq_preflight_max_tokens=500,
        pinecone_api_key="pc-test",
        pinecone_index_name="reved-index",
        pinecone_dimension=384,
        pinecone_metric="cosine",
        pinecone_cloud="aws",
        pinecone_region="us-east-1",
        pinecone_namespace="textbooks",
        pinecone_upsert_batch_size=100,
        pinecone_include_chunk_text=False,
        database_url="",
        database_sync_url="",
        database_pool_size=5,
        database_max_overflow=10,
        supabase_url="",
        supabase_anon_key="",
        supabase_service_role_key="",
        supabase_jwt_secret="",
        supabase_jwt_algorithm="HS256",
        supabase_jwt_audience="authenticated",
        auth_enabled=False,
        cache_backend="memory",
        cache_default_ttl_seconds=300,
        redis_url=None,
        rate_limit_default_tier="free",
        rate_limit_llm_tiers=(),
    )


@pytest.fixture(autouse=True)
def _isolate_engine_cache(monkeypatch: pytest.MonkeyPatch):
    """Point get_settings() at a DB-less Settings and reset the engine cache.

    Mirrors the cache-clear discipline the db_session fixture in
    tests/conftest.py uses for the real-Postgres path -- without it, a
    prior test's cached engine (or this test's) would leak across tests
    via the module-level lru_cache.
    """

    session_module.get_engine.cache_clear()
    monkeypatch.setattr(session_module, "get_settings", _settings_without_database_url)
    yield
    session_module.get_engine.cache_clear()


def test_get_engine_raises_configuration_error_without_database_url() -> None:
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        session_module.get_engine()


async def test_dispose_engine_is_a_noop_when_no_engine_was_created() -> None:
    await session_module.dispose_engine()  # must not raise
