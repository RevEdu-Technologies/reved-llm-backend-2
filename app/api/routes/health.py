"""Health-check route for the API."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_engine
from app.schemas.common import APIResponse
from app.utils.cache import get_cache
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _check_database() -> dict[str, object]:
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("DB health check failed: %s", exc)
        return {"status": "unavailable", "error": str(exc)[:200]}


async def _check_cache() -> dict[str, object]:
    try:
        cache = get_cache()
        ok = await cache.ping()
        return {"status": "ok" if ok else "unavailable"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cache health check failed: %s", exc)
        return {"status": "unavailable", "error": str(exc)[:200]}


@router.get(
    "/health",
    response_model=APIResponse[dict],
    summary="Liveness probe",
    description="Lightweight health check — returns OK if the process is up.",
)
async def health_check() -> APIResponse[dict]:
    return success_response(
        role="system",
        data={"status": "ok"},
        message="Service is healthy.",
    )


@router.get(
    "/health/ready",
    response_model=APIResponse[dict],
    summary="Readiness probe",
    description="Reports downstream dependency health (database, cache, config).",
)
async def readiness() -> APIResponse[dict]:
    settings = get_settings()
    db_check, cache_check = await asyncio.gather(_check_database(), _check_cache())
    ready = db_check.get("status") == "ok" and cache_check.get("status") == "ok"
    payload: dict[str, object] = {
        "status": "ok" if ready else "degraded",
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "cache_backend": settings.cache_backend,
        "checks": {
            "database": db_check,
            "cache": cache_check,
            "config": {
                "pinecone_index": settings.pinecone_index_name,
                "groq_model": settings.groq_model,
                "embedding_model": settings.hf_embedding_model,
            },
        },
    }
    message = "All systems operational." if ready else "One or more dependencies are degraded."
    return success_response(role="system", data=payload, message=message)
