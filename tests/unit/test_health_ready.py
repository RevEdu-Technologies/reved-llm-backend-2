"""Unit tests for the GET /health/ready readiness logic.

Exercises app.api.routes.health.readiness() directly (no HTTP client, no
real Postgres) by monkeypatching its database/cache checks -- the route's
own gathered calls -- so the three cases (DB down, DB up + cache down, both
up) are deterministic. Regression coverage for the 503-storm caused by the
old always-200 readiness response never signaling infra probes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.routes import health as health_module


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        environment="test",
        auth_enabled=False,
        cache_backend="memory",
        pinecone_index_name="reved-index",
        groq_model="llama-3.3-70b-versatile",
        hf_embedding_model="BAAI/bge-base-en-v1.5",
    )
    monkeypatch.setattr(health_module, "get_settings", lambda: settings)


async def test_readiness_returns_503_when_database_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _db_down():
        return {"status": "unavailable", "error": "connection refused"}

    async def _cache_up():
        return {"status": "ok"}

    monkeypatch.setattr(health_module, "_check_database", _db_down)
    monkeypatch.setattr(health_module, "_check_cache", _cache_up)

    response = await health_module.readiness()

    assert response.status_code == 503
    import json

    body = json.loads(response.body)
    assert body["status"] == "error"
    assert body["data"]["code"] == "upstream_error"
    assert body["data"]["details"]["database"]["status"] == "unavailable"


async def test_readiness_returns_200_when_db_up_and_cache_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _db_up():
        return {"status": "ok"}

    async def _cache_down():
        return {"status": "unavailable", "error": "timeout"}

    monkeypatch.setattr(health_module, "_check_database", _db_up)
    monkeypatch.setattr(health_module, "_check_cache", _cache_down)

    response = await health_module.readiness()

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert body["status"] == "success"
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"]["cache"]["status"] == "unavailable"


async def test_readiness_returns_200_when_everything_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok():
        return {"status": "ok"}

    monkeypatch.setattr(health_module, "_check_database", _ok)
    monkeypatch.setattr(health_module, "_check_cache", _ok)

    response = await health_module.readiness()

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert body["data"]["status"] == "ok"
