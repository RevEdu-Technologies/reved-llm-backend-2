"""Unit tests for ``app.core.tracing`` — W3C trace context primitives.

Three concerns covered:

1. The parser/formatter pair handles real W3C ``traceparent`` headers
   and rejects malformed input.
2. ``TraceContextMiddleware`` mints fresh IDs when no inbound header is
   present, honors the inbound ``trace_id`` (and treats inbound
   ``span_id`` as the *parent*, minting a fresh local span), and echoes
   ``traceparent`` on the response.
3. ``start_span`` and ``run_in_thread`` preserve trace context across
   logical-span and thread boundaries.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.tracing import (
    _span_id_ctx,
    _trace_id_ctx,
    current_span_id,
    current_trace_id,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    run_in_thread,
    start_span,
)


# --- Pure helpers --------------------------------------------------------


def test_new_trace_id_is_32_hex():
    tid = new_trace_id()
    assert len(tid) == 32
    int(tid, 16)  # raises on non-hex


def test_new_span_id_is_16_hex():
    sid = new_span_id()
    assert len(sid) == 16
    int(sid, 16)


def test_new_ids_are_unique():
    assert new_trace_id() != new_trace_id()
    assert new_span_id() != new_span_id()


def test_parse_traceparent_happy_path():
    raw = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    parsed = parse_traceparent(raw)
    assert parsed == (
        "0af7651916cd43dd8448eb211c80319c",
        "b7ad6b7169203331",
        "01",
    )


def test_parse_traceparent_uppercase_normalized():
    raw = "00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01"
    parsed = parse_traceparent(raw)
    assert parsed is not None
    assert parsed[0] == "0af7651916cd43dd8448eb211c80319c"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-a-traceparent",
        "00-tooShort-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331",  # missing flags
        # All-zero trace id — spec-invalid
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        # All-zero span id — spec-invalid
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
        # Non-hex chars
        "00-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-b7ad6b7169203331-01",
    ],
)
def test_parse_traceparent_rejects_malformed(raw):
    assert parse_traceparent(raw) is None


def test_format_traceparent_round_trips():
    tid = new_trace_id()
    sid = new_span_id()
    formatted = format_traceparent(tid, sid)
    assert parse_traceparent(formatted) == (tid, sid, "01")


# --- start_span ----------------------------------------------------------


def test_start_span_pushes_and_restores():
    """A nested span gets a fresh id and yields control back to the parent."""

    outer_token = _span_id_ctx.set("outer1234abcdef0")
    try:
        assert current_span_id() == "outer1234abcdef0"
        with start_span("test.unit") as sid:
            assert current_span_id() == sid
            assert sid != "outer1234abcdef0"
        # restored after the with block exits
        assert current_span_id() == "outer1234abcdef0"
    finally:
        _span_id_ctx.reset(outer_token)


def test_start_span_attributes_attach_to_otel_span():
    """When OTEL is configured, ``start_span`` keyword args land on the span.

    OTEL only allows one TracerProvider per process, and the conftest
    pins ``OTEL_EXPORTER=none`` so the suite-wide provider has zero
    processors. We add a temporary ``InMemorySpanExporter`` processor
    to capture spans for this test, then remove it. Importing
    ``app.core.tracing`` ensures ``configure_tracing`` has run (via
    the main app import elsewhere) and ``_configured`` is True so
    ``start_span`` takes the OTEL branch.
    """

    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Make sure main app has been imported so configure_tracing ran.
    import main  # noqa: F401
    import app.core.tracing as _tracing_module

    provider = _trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        pytest.skip(
            "Global tracer provider is not the SDK TracerProvider — "
            "another test probably set up a ProxyTracerProvider; skip "
            "rather than fight global state."
        )

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    # Re-resolve the module-level tracer in case a previous test
    # bound it to a stale provider snapshot.
    _tracing_module._tracer = _trace.get_tracer("reved.app")
    assert _tracing_module._configured, "configure_tracing should have run via main import"

    try:
        with start_span("rag.embed_query", **{"rag.query_length": 42}):
            pass
        spans = exporter.get_finished_spans()
        # Multiple spans may be in flight from instrumented libraries;
        # find ours by name.
        ours = [s for s in spans if s.name == "rag.embed_query"]
        assert len(ours) == 1
        assert ours[0].attributes.get("rag.query_length") == 42
    finally:
        # Remove the processor and drain so the next test starts clean.
        processor.shutdown()
        exporter.clear()


def test_start_span_yields_active_span_id():
    """The yielded value matches what ``current_span_id`` reports inside."""

    with start_span("test.unit") as sid:
        # Either the OTEL span id (configured path) or a contextvar
        # value (fallback path) — same shape: 16 hex.
        assert sid == current_span_id()
        assert len(sid) == 16


# --- async / thread propagation ------------------------------------------


async def test_context_propagates_to_create_task():
    """asyncio.create_task copies contextvars at task-creation time."""

    trace_token = _trace_id_ctx.set("a" * 32)
    span_token = _span_id_ctx.set("b" * 16)
    try:
        captured: dict[str, str | None] = {}

        async def _inside():
            captured["trace"] = current_trace_id()
            captured["span"] = current_span_id()

        await asyncio.create_task(_inside())
        assert captured == {"trace": "a" * 32, "span": "b" * 16}
    finally:
        _span_id_ctx.reset(span_token)
        _trace_id_ctx.reset(trace_token)


async def test_run_in_thread_propagates_context():
    """Thread offload via run_in_thread must carry trace context too."""

    trace_token = _trace_id_ctx.set("c" * 32)
    span_token = _span_id_ctx.set("d" * 16)
    try:
        def _on_thread():
            return current_trace_id(), current_span_id()

        result = await run_in_thread(_on_thread)
        assert result == ("c" * 32, "d" * 16)
    finally:
        _span_id_ctx.reset(span_token)
        _trace_id_ctx.reset(trace_token)


async def test_context_isolated_between_concurrent_tasks():
    """Two tasks running concurrently must each see their own trace context."""

    async def _task(tid: str, sid: str, capture: dict):
        _trace_id_ctx.set(tid)
        _span_id_ctx.set(sid)
        # Yield so the other task interleaves before we read back.
        await asyncio.sleep(0)
        capture["trace"] = current_trace_id()
        capture["span"] = current_span_id()

    cap_a: dict = {}
    cap_b: dict = {}
    await asyncio.gather(
        _task("a" * 32, "1" * 16, cap_a),
        _task("b" * 32, "2" * 16, cap_b),
    )
    assert cap_a == {"trace": "a" * 32, "span": "1" * 16}
    assert cap_b == {"trace": "b" * 32, "span": "2" * 16}


# --- OTEL FastAPI instrumentation end-to-end -----------------------------
#
# N1 shipped a hand-rolled ``TraceContextMiddleware`` that echoed
# ``traceparent`` on the response. N2 hands trace-context ownership to
# OTEL's FastAPI instrumentation, which:
#
#   * parses inbound ``traceparent`` per W3C (so we join the caller's
#     trace), and
#   * creates a server span per request whose ``trace_id`` and
#     ``span_id`` are reachable via ``current_trace_id()`` /
#     ``current_span_id()`` inside the handler scope.
#
# OTEL's standard practice does NOT echo a ``traceparent`` response
# header (it's an outbound header for client→server propagation). So
# we assert the meaningful property indirectly: the ``reved.access``
# log line for the request carries the expected trace_id.


import io
import json
import logging as _logging


def _capture_access_lines() -> tuple[io.StringIO, callable]:
    """Set up a stub access-log handler; returns (buffer, restore)."""

    buf = io.StringIO()
    handler = _logging.StreamHandler(buf)

    class _F(_logging.Formatter):
        def format(self, r):
            return json.dumps(
                {k: getattr(r, k, None) for k in
                 ("trace_id", "span_id", "request_id", "path", "status")}
            )

    handler.setFormatter(_F())
    log = _logging.getLogger("reved.access")
    original = (list(log.handlers), log.level, log.propagate)
    log.handlers = [handler]
    log.setLevel(_logging.INFO)
    log.propagate = False

    def _restore():
        log.handlers, log.level, log.propagate = original

    return buf, _restore


async def test_fastapi_instrumentation_mints_a_trace_per_request():
    from main import app

    buf, restore = _capture_access_lines()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})
        line = json.loads(buf.getvalue().splitlines()[0])
        # OTEL surfaces both IDs through ``current_trace_id`` /
        # ``current_span_id``; the access log uses them directly.
        assert line["trace_id"] is not None
        assert line["span_id"] is not None
        assert len(line["trace_id"]) == 32
        assert len(line["span_id"]) == 16
        # Non-zero IDs (OTEL rejects all-zero per spec).
        assert int(line["trace_id"], 16) != 0
    finally:
        restore()


async def test_fastapi_instrumentation_honors_inbound_traceparent():
    from main import app

    inbound_trace = "0af7651916cd43dd8448eb211c80319c"
    inbound_span = "b7ad6b7169203331"
    inbound = f"00-{inbound_trace}-{inbound_span}-01"

    buf, restore = _capture_access_lines()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get(
                "/api/v1/parent/child-activity",
                headers={"X-Dev-Role": "parent", "traceparent": inbound},
            )
        line = json.loads(buf.getvalue().splitlines()[0])
        # Trace continues — same trace_id as the inbound header.
        assert line["trace_id"] == inbound_trace
        # But our span is fresh — the inbound id was the *parent*.
        assert line["span_id"] != inbound_span
        assert len(line["span_id"]) == 16
    finally:
        restore()


async def test_fastapi_instrumentation_rejects_malformed_traceparent():
    """A malformed inbound header is dropped; we mint a fresh trace."""

    from main import app

    buf, restore = _capture_access_lines()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            await c.get(
                "/api/v1/parent/child-activity",
                headers={"X-Dev-Role": "parent", "traceparent": "garbage"},
            )
        line = json.loads(buf.getvalue().splitlines()[0])
        # Still got a valid trace, even though the inbound was bogus.
        assert line["trace_id"] is not None
        assert int(line["trace_id"], 16) != 0
    finally:
        restore()


async def test_trace_context_clears_after_request():
    """Outside a handler, current_trace_id()/current_span_id() must be None."""

    from main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/api/v1/parent/child-activity", headers={"X-Dev-Role": "parent"})
    assert current_trace_id() is None
    assert current_span_id() is None
