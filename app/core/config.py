"""Application configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.secrets import load_secret


class ConfigurationError(RuntimeError):
    """Raised when required environment configuration is missing or invalid."""


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env", override=False)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _require_secret(name: str, fallback_env: str) -> str:
    """Resolve a secret via the secrets manager (or env fallback) and require it."""

    value = load_secret(name, fallback_env)
    if not value:
        raise ConfigurationError(
            f"Missing required secret: {name} (env fallback: {fallback_env}). "
            "Configure the value in your secrets backend or set the env var."
        )
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"Environment variable {name} must be an integer.") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"Environment variable {name} must be a boolean.")


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_tier_limit_map(name: str) -> tuple[tuple[str, str], ...]:
    """Parse a ``tier:limit`` override map from an env var.

    Format: ``free:10/minute,premium:60/minute,unlimited:1000/minute``.
    Limit strings are slowapi rate-limit expressions (``;``-separated for
    multiple windows, e.g. ``60/minute;1000/hour``). Returns the overrides
    only — ``app.core.rate_limit`` layers them on top of its built-in
    defaults so an empty env var keeps the defaults. Malformed entries
    (no ``:`` separator) are skipped rather than raising, so a typo in one
    tier never takes the whole service down.
    """

    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return ()
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        tier, _, limit = item.partition(":")
        tier = tier.strip().lower()
        limit = limit.strip()
        if tier and limit:
            pairs[tier] = limit
    return tuple(pairs.items())


_SYNC_DB_SCHEMES = {"postgresql", "postgres"}
_ASYNC_DB_SCHEMES = {"postgresql+asyncpg", "postgres+asyncpg"}


def _as_async_db_url(raw: str) -> str:
    """Normalize a Postgres URL for SQLAlchemy async (asyncpg driver)."""

    if not raw:
        return raw
    if raw.startswith(("postgresql+asyncpg://", "postgres+asyncpg://")):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw[len("postgres://") :]
    return raw


def _as_sync_db_url(raw: str) -> str:
    """Normalize a Postgres URL for Alembic/sync tooling (psycopg3 driver)."""

    if not raw:
        return raw
    if raw.startswith(("postgresql+asyncpg://", "postgres+asyncpg://")):
        return "postgresql+psycopg://" + raw.split("://", 1)[1]
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://") :]
    return raw


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-backed application settings."""

    environment: str
    cors_allowed_origins: tuple[str, ...]
    huggingface_api_key: str
    embedding_backend: str
    hf_embedding_model: str
    hf_embedding_batch_size: int
    hf_embedding_normalize: bool
    embedding_query_prefix: str
    embedding_passage_prefix: str
    embedding_device: str
    groq_api_key: str
    groq_model: str
    groq_temperature: float
    groq_max_completion_tokens: int
    groq_preflight_model: str
    groq_preflight_max_tokens: int
    pinecone_api_key: str
    pinecone_index_name: str
    pinecone_dimension: int
    pinecone_metric: str
    pinecone_cloud: str
    pinecone_region: str
    pinecone_namespace: str
    pinecone_upsert_batch_size: int
    pinecone_include_chunk_text: bool
    database_url: str
    database_sync_url: str
    database_pool_size: int
    database_max_overflow: int
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    supabase_jwt_secret: str
    supabase_jwt_algorithm: str
    supabase_jwt_audience: str
    auth_enabled: bool
    cache_backend: str
    cache_default_ttl_seconds: int
    redis_url: str | None
    rate_limit_default_tier: str
    rate_limit_llm_tiers: tuple[tuple[str, str], ...]

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""

        _load_dotenv_if_available()
        # Local dev origins. 3000 is the historical default; 8080 is the
        # RevEd frontend's Vite dev server (see reved-technologies
        # vite.config.ts). 5173 is Vite's own default in case the port is
        # left unset. Deployed origins are supplied via CORS_ALLOWED_ORIGINS.
        default_origins = [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]

        raw_database_url = _require_env("DATABASE_URL")
        settings = cls(
            environment=os.getenv("ENVIRONMENT", "development").strip() or "development",
            cors_allowed_origins=tuple(_get_list("CORS_ALLOWED_ORIGINS", default_origins)),
            huggingface_api_key=os.getenv("HUGGINGFACE_API_KEY", "").strip(),
            embedding_backend=os.getenv("EMBEDDING_BACKEND", "local").strip().lower() or "local",
            hf_embedding_model=os.getenv(
                "HF_EMBEDDING_MODEL",
                "BAAI/bge-base-en-v1.5",
            ).strip(),
            hf_embedding_batch_size=_get_int("HF_EMBEDDING_BATCH_SIZE", 32),
            hf_embedding_normalize=_get_bool("HF_EMBEDDING_NORMALIZE", True),
            embedding_query_prefix=os.getenv(
                "EMBEDDING_QUERY_PREFIX",
                "Represent this sentence for searching relevant passages: ",
            ),
            embedding_passage_prefix=os.getenv("EMBEDDING_PASSAGE_PREFIX", ""),
            embedding_device=os.getenv("EMBEDDING_DEVICE", "cpu").strip() or "cpu",
            groq_api_key=_require_secret("groq_api_key", "GROQ_API_KEY"),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
            groq_temperature=float(os.getenv("GROQ_TEMPERATURE", "0.1")),
            groq_max_completion_tokens=_get_int("GROQ_MAX_COMPLETION_TOKENS", 700),
            groq_preflight_model=os.getenv(
                "GROQ_PREFLIGHT_MODEL", "llama-3.3-70b-versatile"
            ).strip()
            or "llama-3.3-70b-versatile",
            groq_preflight_max_tokens=_get_int("GROQ_PREFLIGHT_MAX_TOKENS", 500),
            pinecone_api_key=_require_secret("pinecone_api_key", "PINECONE_API_KEY"),
            pinecone_index_name=os.getenv("PINECONE_INDEX_NAME", "reved-index-v2").strip(),
            pinecone_dimension=_get_int("PINECONE_DIMENSION", 768),
            pinecone_metric=os.getenv("PINECONE_METRIC", "cosine").strip() or "cosine",
            pinecone_cloud=os.getenv("PINECONE_CLOUD", "aws").strip() or "aws",
            pinecone_region=os.getenv("PINECONE_REGION", "us-east-1").strip() or "us-east-1",
            pinecone_namespace=os.getenv("PINECONE_NAMESPACE", "textbooks").strip() or "textbooks",
            pinecone_upsert_batch_size=_get_int("PINECONE_UPSERT_BATCH_SIZE", 100),
            pinecone_include_chunk_text=_get_bool("PINECONE_INCLUDE_CHUNK_TEXT", False),
            database_url=_as_async_db_url(raw_database_url),
            database_sync_url=_as_sync_db_url(raw_database_url),
            database_pool_size=_get_int("DATABASE_POOL_SIZE", 5),
            database_max_overflow=_get_int("DATABASE_MAX_OVERFLOW", 10),
            supabase_url=os.getenv("SUPABASE_URL", "").strip(),
            supabase_anon_key=os.getenv("SUPABASE_ANON_KEY", "").strip(),
            supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
            supabase_jwt_secret=load_secret("supabase_jwt_secret", "SUPABASE_JWT_SECRET"),
            supabase_jwt_algorithm=os.getenv("SUPABASE_JWT_ALGORITHM", "HS256").strip() or "HS256",
            supabase_jwt_audience=os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated").strip() or "authenticated",
            auth_enabled=_get_bool("AUTH_ENABLED", False),
            cache_backend=os.getenv("CACHE_BACKEND", "memory").strip().lower() or "memory",
            cache_default_ttl_seconds=_get_int("CACHE_DEFAULT_TTL_SECONDS", 300),
            redis_url=os.getenv("REDIS_URL", "").strip() or None,
            rate_limit_default_tier=os.getenv("RATE_LIMIT_DEFAULT_TIER", "free").strip().lower()
            or "free",
            rate_limit_llm_tiers=_get_tier_limit_map("RATE_LIMIT_LLM_TIERS"),
        )
        settings.validate()
        return settings

    @property
    def is_production_like(self) -> bool:
        """True when ENVIRONMENT names any non-local environment."""

        return self.environment.strip().lower() in {"production", "prod", "staging"}

    def validate(self) -> None:
        """Validate the loaded configuration."""

        if self.is_production_like and not self.auth_enabled:
            raise ConfigurationError(
                "ENVIRONMENT="
                f"{self.environment} requires AUTH_ENABLED=true. "
                "Refusing to start with authentication disabled in a non-development environment."
            )
        if self.embedding_backend not in {"local", "hf_api"}:
            raise ConfigurationError("EMBEDDING_BACKEND must be 'local' or 'hf_api'.")
        if self.embedding_backend == "hf_api" and not self.huggingface_api_key:
            raise ConfigurationError("EMBEDDING_BACKEND=hf_api requires HUGGINGFACE_API_KEY.")
        if self.pinecone_dimension <= 0:
            raise ConfigurationError("PINECONE_DIMENSION must be greater than zero.")
        if self.hf_embedding_batch_size <= 0:
            raise ConfigurationError("HF_EMBEDDING_BATCH_SIZE must be greater than zero.")
        if self.groq_max_completion_tokens <= 0:
            raise ConfigurationError("GROQ_MAX_COMPLETION_TOKENS must be greater than zero.")
        if self.groq_temperature < 0:
            raise ConfigurationError("GROQ_TEMPERATURE cannot be negative.")
        if self.pinecone_upsert_batch_size <= 0:
            raise ConfigurationError("PINECONE_UPSERT_BATCH_SIZE must be greater than zero.")
        if self.pinecone_metric != "cosine":
            raise ConfigurationError("PINECONE_METRIC must be 'cosine' for the MVP.")
        if self.database_pool_size <= 0:
            raise ConfigurationError("DATABASE_POOL_SIZE must be greater than zero.")
        if self.cache_backend not in {"memory", "redis"}:
            raise ConfigurationError("CACHE_BACKEND must be 'memory' or 'redis'.")
        if self.cache_backend == "redis" and not self.redis_url:
            raise ConfigurationError("CACHE_BACKEND=redis requires REDIS_URL.")
        if self.auth_enabled and not self.supabase_jwt_secret:
            raise ConfigurationError("AUTH_ENABLED=true requires SUPABASE_JWT_SECRET.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings.from_env()
