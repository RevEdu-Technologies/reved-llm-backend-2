"""Error responses must carry CORS headers, or browsers report them as CORS failures."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from main import create_app


@pytest.fixture
def origin() -> str:
    return get_settings().cors_allowed_origins[0]


@pytest.fixture
def app_with_failing_route():
    app = create_app()

    @app.get("/__boom")
    async def boom():
        raise RuntimeError("boom")

    return app


async def test_unhandled_500_has_cors_header(app_with_failing_route, origin):
    transport = ASGITransport(app=app_with_failing_route, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/__boom", headers={"Origin": origin})
    assert res.status_code == 500
    assert res.headers.get("access-control-allow-origin") == origin
    assert res.json()["status"] == "error"


async def test_unhandled_500_omits_cors_for_unknown_origin(app_with_failing_route):
    transport = ASGITransport(app=app_with_failing_route, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/__boom", headers={"Origin": "https://evil.example"})
    assert res.status_code == 500
    assert "access-control-allow-origin" not in res.headers


async def test_cors_wraps_middleware_that_can_answer_on_its_own():
    """SlowAPI's 429s and the request middlewares' responses must pass through CORS."""
    from fastapi.middleware.cors import CORSMiddleware
    from slowapi.middleware import SlowAPIMiddleware

    app = create_app()
    order = [m.cls for m in app.user_middleware]  # outermost first
    assert order.index(CORSMiddleware) < order.index(SlowAPIMiddleware)
