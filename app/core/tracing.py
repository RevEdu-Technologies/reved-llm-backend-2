"""Trace context backed by OpenTelemetry.

N1 shipped a hand-rolled implementation of ``trace_id`` / ``span_id``
contextvars + a ``TraceContextMiddleware`` so log lines could carry
W3C correlation IDs before the OTEL SDK landed. N2 (this module's
current form) swaps the internals for OTEL while keeping the public
surface unchanged:

    current_trace_id() / current_span_id()    — read OTEL's active span
    start_span(name)                          — wraps tracer.start_as_current_span
    run_in_thread(func, *args)                — asyncio.to_thread shim
    parse_traceparent / format_traceparent    — W3C header utility (kept;
                                                 no longer used internally)

The auto-instrumentation packages cover FastAPI requests, SQLAlchemy
queries, outbound httpx (which Groq uses), and asyncpg. The Pinecone
SDK is wrapped manually in ``app/rag/retrieval/retriever.py`` because
it doesn't ride httpx.

Exporter selection (env var ``OTEL_EXPORTER``, default ``console``):
    none     — no-op; tests use this by default via the conftest.
    console  — spans pretty-printed to stdout. The dev default.
    otlp     — OTLP/HTTP exporter; reads OTEL_EXPORTER_OTLP_ENDPOINT.

To switch backends (Honeycomb / Datadog / Grafana Cloud / Tempo etc.)
set ``OTEL_EXPORTER=otlp`` and point ``OTEL_EXPORTER_OTLP_ENDPOINT``
at the vendor's OTLP/HTTP receiver. No code change.
"""

from __future__ import annotations

import contextvars
import os
import re
import secrets
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from opentelemetry import trace

_TRACEPARENT_HEADER = "traceparent"

# W3C trace-context: <version>-<trace-id>-<parent-id>-<trace-flags>
_TRACEPARENT_PATTERN = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_ALL_ZERO_TRACE = "0" * 32
_ALL_ZERO_SPAN = "0" * 16
_DEFAULT_FLAGS = "01"

_tracer = trace.get_tracer("reved.app")

# Configuration flag — set by ``configure_tracing`` so we don't try to
# instrument the same provider twice in long-lived test processes.
_configured = False

# Legacy contextvars kept as a fallback so spans created OUTSIDE an OTEL
# tracer (e.g. tests that build the app with OTEL disabled) can still
# read a trace context, and so ``start_span`` has somewhere to push a
# span ID when no recording span is active. The CorrelationFilter
# prefers the OTEL view when available.
_trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reved_trace_id", default=None
)
_span_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reved_span_id", default=None
)


# --- Public read API ------------------------------------------------------


def current_trace_id() -> str | None:
    """Return the active W3C trace id (32 hex), or None.

    Reads the OTEL span context first; falls back to the legacy
    contextvar so tests that pin a trace id manually still see it.
    """

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return _trace_id_ctx.get()


def current_span_id() -> str | None:
    """Return the active W3C span id (16 hex), or None."""

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return format(ctx.span_id, "016x")
    return _span_id_ctx.get()


# --- ID minting -----------------------------------------------------------


def new_trace_id() -> str:
    """Mint a fresh W3C trace id (32 hex)."""

    return secrets.token_hex(16)


def new_span_id() -> str:
    """Mint a fresh W3C span id (16 hex)."""

    return secrets.token_hex(8)


# --- traceparent header utility ------------------------------------------
#
# OTEL's FastAPI instrumentation parses inbound ``traceparent`` headers
# automatically via the configured TextMapPropagator. These helpers stay
# exported for any caller that wants to parse/build the header manually
# (e.g. a script that calls out to a sibling service).


def parse_traceparent(value: str) -> tuple[str, str, str] | None:
    if not value:
        return None
    m = _TRACEPARENT_PATTERN.match(value.strip().lower())
    if not m:
        return None
    _version, trace_id, parent_id, flags = m.groups()
    if trace_id == _ALL_ZERO_TRACE or parent_id == _ALL_ZERO_SPAN:
        return None
    return trace_id, parent_id, flags


def format_traceparent(
    trace_id: str, span_id: str, flags: str = _DEFAULT_FLAGS
) -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


# --- Span helper ----------------------------------------------------------


@contextmanager
def start_span(name: str, **attributes: object) -> Iterator[str]:
    """Push a child span and yield its 16-hex span id.

    A thin wrapper around OTEL's ``tracer.start_as_current_span`` so
    call sites read naturally and the contract — "this block is its
    own logical span" — stays decoupled from the SDK.

    Falls back to a contextvar-only push when no OTEL tracer is active
    (e.g. tests with ``OTEL_EXPORTER=none``), so call sites work the
    same in production and in test.
    """

    if _configured:
        with _tracer.start_as_current_span(name) as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)  # type: ignore[arg-type]
            ctx = span.get_span_context()
            yield format(ctx.span_id, "016x")
    else:
        sid = new_span_id()
        token = _span_id_ctx.set(sid)
        try:
            yield sid
        finally:
            _span_id_ctx.reset(token)


# --- Cross-thread propagation --------------------------------------------

_T = TypeVar("_T")


async def run_in_thread(
    func: Callable[..., _T],
    /,
    *args: object,
    **kwargs: object,
) -> _T:
    """Run ``func`` on the default executor with trace context propagated.

    ``asyncio.to_thread`` already copies contextvars (Python 3.10+), and
    OTEL's default context manager uses contextvars under the hood, so
    spans started inside the thread inherit the caller's parent. This
    wrapper exists so the helper for "trace-aware thread offload" lives
    in one place — a grep for ``asyncio.to_thread`` outside this module
    flags places that may have skipped it.
    """

    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)


# --- Setup ---------------------------------------------------------------


def _exporter_choice() -> str:
    """Resolve the OTEL exporter from env var, default ``console``.

    Tests set ``OTEL_EXPORTER=none`` in the conftest so spans don't spam
    stdout during runs. Production deploys set ``OTEL_EXPORTER=otlp``
    and ``OTEL_EXPORTER_OTLP_ENDPOINT``.
    """

    raw = (os.getenv("OTEL_EXPORTER") or "console").strip().lower()
    if raw in {"none", "off", "disabled"}:
        return "none"
    if raw in {"console", "stdout"}:
        return "console"
    if raw in {"otlp", "otlp-http", "otlp-grpc"}:
        return "otlp"
    return "console"


def configure_tracing(app=None) -> None:
    """Install OTEL tracer + auto-instrumentations. Idempotent.

    Call once at app startup. ``app`` is the FastAPI instance — passed
    so we can wire ``FastAPIInstrumentor.instrument_app(...)`` here
    rather than expose the instrumentor at the call site. Pass ``None``
    to set up only the tracer + library instrumentations (useful from
    tests).

    Three exporter modes (env var ``OTEL_EXPORTER``):

    * ``console`` (default) — spans pretty-print to stdout.
    * ``otlp`` — OTLP/HTTP exporter (reads OTEL_EXPORTER_OTLP_ENDPOINT).
    * ``none`` — spans are still **created** (so trace_id / span_id
      appear on log lines, requests still have a parent span) but
      **never exported**. Tests use this mode by default.
    """

    global _configured

    exporter = _exporter_choice()

    if not _configured:
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        from app.core.config import get_settings

        settings = get_settings()
        resource = Resource.create(
            {
                SERVICE_NAME: "reved-backend",
                "service.version": "0.1.0",
                "deployment.environment": settings.environment,
            }
        )
        provider = TracerProvider(resource=resource)

        if exporter == "console":
            # SimpleSpanProcessor exports synchronously — fine for dev,
            # spans show up immediately. Don't use in production: it
            # blocks the request thread on each export.
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        elif exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            # OTLPSpanExporter respects OTEL_EXPORTER_OTLP_ENDPOINT,
            # OTEL_EXPORTER_OTLP_HEADERS, OTEL_EXPORTER_OTLP_TIMEOUT
            # out of the box — no kwargs needed.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        # exporter == "none": tracer provider is set up but no
        # processors — spans get IDs and propagate normally but are
        # never exported. The N1 contract (trace_id on every log line)
        # is preserved without producing any side effects.

        trace.set_tracer_provider(provider)

        # Library auto-instrumentation. These are idempotent themselves
        # but cheap; gating on _configured keeps test re-imports tidy.
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        AsyncPGInstrumentor().instrument()

        _configured = True

    # FastAPI instrumentation is per-app; OTEL's instrumentor handles
    # idempotency itself if called twice. Excluded URLs use the same
    # observability skip-set as the access log + Prometheus.
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        from app.core.logging import OBSERVABILITY_EXCLUDED_PATHS

        # FastAPIInstrumentor wants a comma-separated regex string.
        excluded = ",".join(re.escape(p) for p in sorted(OBSERVABILITY_EXCLUDED_PATHS))

        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded)


__all__ = [
    "configure_tracing",
    "current_span_id",
    "current_trace_id",
    "format_traceparent",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "run_in_thread",
    "start_span",
]
