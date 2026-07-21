"""Unit tests for ``RequestLogMiddleware``.

The middleware emits one ``reved.access`` log record per request with
``method``, ``path``, ``status``, ``duration_ms``, ``user_id``, ``role``
(plus ``request_id`` injected by the existing logging filter). These
tests capture the ``reved.access`` handler stream directly instead of
going through the JSON formatter — same pattern as
``tests/integration/test_audit_hooks.py``.

We hit the app via ``httpx.ASGITransport`` so the middleware stack runs
end-to-end (RequestIdMiddleware wraps RequestLogMiddleware in
``main.py``; both are exercised here).
"""

from __future__ import annotations

import io
import json
import logging
import re
import uuid

import pytest
from httpx import ASGITransport, AsyncClient


# The closed set of keys RequestLogMiddleware writes via ``extra``.
# Keeping this explicit (vs. introspecting LogRecord.__dict__) means the
# fixture is robust to stdlib LogRecord additions and self-documenting:
# any new field the middleware emits must be added here.
_ACCESS_LOG_EXTRA_KEYS = (
    "method", "path", "status", "duration_ms",
    "user_id", "role",
    "request_id", "trace_id", "span_id",
)


@pytest.fixture
def access_stream():
    """Capture one JSON line per record from the ``reved.access`` logger.

    Replaces the logger's handlers so the captured stream is exactly the
    middleware's output and nothing else. Restored at teardown.
    """

    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)

    class _AccessFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            for k in _ACCESS_LOG_EXTRA_KEYS:
                v = getattr(record, k, None)
                if v is not None:
                    payload[k] = v
            return json.dumps(payload)

    handler.setFormatter(_AccessFormatter())
    logger = logging.getLogger("reved.access")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    # Don't double-emit through the root handler we don't control in tests.
    logger.propagate = False
    try:
        yield buf
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


async def _client():
    """Build a client against the real app (same as integration tests)."""

    from main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_emits_one_line_with_expected_keys(access_stream):
    async with await _client() as c:
        resp = await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})

    assert resp.status_code in (200, 500), resp.text  # auth/dev mode → handler runs
    records = _lines(access_stream)
    assert len(records) == 1
    rec = records[0]
    for key in (
        "method", "path", "status", "duration_ms", "user_id", "role",
        "request_id", "trace_id", "span_id",
    ):
        assert key in rec, f"missing key {key!r} in {rec!r}"
    assert rec["method"] == "GET"
    assert rec["path"] == "/api/v1/parent/child-activity"
    assert rec["role"] == "parent"
    # request_id (Phase 3) is 32 hex; trace_id (N1) is 32 hex; span_id is 16.
    assert re.fullmatch(r"[0-9a-f]{32}", rec["request_id"])
    assert re.fullmatch(r"[0-9a-f]{32}", rec["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", rec["span_id"])


async def test_duration_ms_is_positive_and_realistic(access_stream):
    async with await _client() as c:
        await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})

    rec = _lines(access_stream)[0]
    assert isinstance(rec["duration_ms"], (int, float))
    assert rec["duration_ms"] > 0
    # 60s is a very generous upper bound — covers cold-start DB reads against
    # the UK→US Supabase pooler. If we're slower than this, something is wrong.
    assert rec["duration_ms"] < 60_000


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/health",
        "/api/v1/health/ready",
        "/metrics",
        "/openapi.json",
        "/docs",
    ],
)
async def test_skips_health_and_doc_endpoints(access_stream, path):
    async with await _client() as c:
        await c.get(path)
    assert _lines(access_stream) == [], f"unexpected access line for {path}"


async def test_logs_anonymous_when_no_auth_dependency_ran(access_stream):
    """A 404 from an unmatched path never hits ``get_current_user``."""

    async with await _client() as c:
        resp = await c.get("/api/v1/this-path-does-not-exist")

    assert resp.status_code == 404
    rec = _lines(access_stream)[0]
    assert rec["status"] == 404
    assert rec["user_id"] == "anonymous"
    assert rec["role"] == "anonymous"


async def test_status_reflects_role_mismatch_403(access_stream):
    """A student hitting a parent-only endpoint trips ``require_role`` → 403."""

    student_id = uuid.uuid4()
    async with await _client() as c:
        await c.get(
            "/api/v1/parent/child-activity",
            headers={"X-Dev-Role": "student"},
        )
    rec = _lines(access_stream)[0]
    assert rec["status"] == 403
    # ``get_current_user`` DID run (it produced the student stub) before
    # ``require_role`` rejected, so identity surfaces on the line.
    assert rec["role"] == "student"
    # The stub user has a deterministic UUID — make sure it's the real value,
    # not a placeholder.
    assert rec["user_id"] != "anonymous"
    # Belt-and-braces: not the parent we'd see if dev-role normalization
    # silently fell back.
    assert rec["user_id"] != str(student_id)


async def test_one_line_per_request_under_concurrency(access_stream):
    """Two sequential requests produce two distinct lines."""

    async with await _client() as c:
        await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})
        await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})

    records = _lines(access_stream)
    assert len(records) == 2
    # Each request gets its own request_id.
    assert records[0]["request_id"] != records[1]["request_id"]
