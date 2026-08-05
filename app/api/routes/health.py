"""Health-check route for the API."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_engine
from app.schemas.common import APIResponse
from app.utils.cache import get_cache
from app.utils.response_builder import error_response, success_response

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
@router.get(
    "/health/live",
    response_model=APIResponse[dict],
    summary="Liveness probe",
    description=(
        "Alias of GET /health for infra that expects an explicit /live path "
        "(e.g. k8s livenessProbe convention). Same behavior: never touches "
        "the database, returns OK whenever the process can serve a request."
    ),
    include_in_schema=False,
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
    description=(
        "Reports downstream dependency health (database, cache, config). "
        "Gates HTTP status on the database only: 200 when the database is "
        "reachable (even if the cache is degraded — cache is a performance "
        "optimization, not a correctness dependency), 503 when the database "
        "is unreachable. Infra readiness probes should key off the status "
        "code; the response body always carries the full per-dependency "
        "breakdown for humans/dashboards."
    ),
)
async def readiness() -> JSONResponse:
    settings = get_settings()
    db_check, cache_check = await asyncio.gather(_check_database(), _check_cache())
    db_ok = db_check.get("status") == "ok"
    cache_ok = cache_check.get("status") == "ok"
    checks = {
        "database": db_check,
        "cache": cache_check,
        "config": {
            "pinecone_index": settings.pinecone_index_name,
            "groq_model": settings.groq_model,
            "embedding_model": settings.hf_embedding_model,
        },
    }

    if not db_ok:
        envelope = error_response(
            role="system",
            code="upstream_error",
            message="Database is unreachable.",
            details=checks,
        )
        return JSONResponse(status_code=503, content=envelope.model_dump(mode="json"))

    payload: dict[str, object] = {
        "status": "ok" if cache_ok else "degraded",
        "environment": settings.environment,
        "auth_enabled": settings.auth_enabled,
        "cache_backend": settings.cache_backend,
        "checks": checks,
    }
    message = "All systems operational." if cache_ok else "Database is healthy; cache is degraded."
    envelope = success_response(role="system", data=payload, message=message)
    return JSONResponse(status_code=200, content=envelope.model_dump(mode="json"))
