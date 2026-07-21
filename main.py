"""RevEd LLM Backend — FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.error_handlers import register_error_handlers
from app.api.routes import api_router
from app.core.config import get_settings
from app.core.i18n import LanguageMiddleware
from app.core.logging import (
    RequestIdMiddleware,
    RequestLogMiddleware,
    configure_logging,
)
from app.core.metrics import install_instrumentator
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.tracing import configure_tracing
from app.db.session import dispose_engine


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""

    configure_logging(level=logging.INFO)

    settings = get_settings()

    if not settings.auth_enabled:
        logging.getLogger(__name__).warning(
            "AUTH DISABLED — the API will accept unauthenticated requests and honor "
            "the X-Dev-Role header. This is intended for local development only; never "
            "deploy with AUTH_ENABLED=false. (ENVIRONMENT=%s)",
            settings.environment,
        )

    app = FastAPI(
        title="RevEd LLM Backend",
        description="Role-aware grounded QA and tutoring API for the RevEd learning platform.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # RequestLogMiddleware sits *inside* RequestIdMiddleware so the
    # access-log line carries the bound request_id; the trade-off is
    # that requests denied by SlowAPI (added below) don't reach
    # RequestLog and therefore don't produce an access line. That's
    # intentional — slowapi 429s are already counted in Prometheus via
    # the audit/rate-limit hooks, and routing them around the access
    # log keeps the line count proportional to "requests that actually
    # ran a handler".
    app.add_middleware(RequestLogMiddleware)

    # LanguageMiddleware binds the Accept-Language-negotiated locale to a
    # contextvar for the duration of the request so service-layer code can
    # localize via app.core.i18n.translate(). Sits inside RequestId (it
    # needs no request_id) and outside RequestLog. Error handlers don't
    # depend on it — they negotiate off the request header directly.
    app.add_middleware(LanguageMiddleware)

    # RequestIdMiddleware wraps RequestLog so the X-Request-Id response
    # header is set even when an outer middleware shortcircuits.
    # Trace context (trace_id + span_id) is owned by OTEL's FastAPI
    # instrumentation, wired below in configure_tracing — that
    # instrumentation creates a span per request, parses inbound
    # ``traceparent``, and surfaces both IDs via current_trace_id() /
    # current_span_id() to the logging filter.
    app.add_middleware(RequestIdMiddleware)

    # Rate limiting. The limiter must be attached to ``app.state`` so the
    # ``@limiter.limit(...)`` decorators on route handlers can locate it,
    # and the middleware applies the default per-caller cap to every
    # endpoint. The exception handler wraps slowapi's 429 in the standard
    # RevEd envelope.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    register_error_handlers(app)
    app.include_router(api_router)

    # Prometheus /metrics. Installs the default request counter +
    # latency histogram on every route, then exposes /metrics in
    # Prometheus text format (excluded from OpenAPI). Domain-specific
    # counters live in app.core.metrics and are populated from the
    # audit hooks + LLM client. See DEPLOY.md §12 for the dashboard
    # and alert rule pointers.
    install_instrumentator(app)

    # OpenTelemetry — tracer provider + auto-instrumentation for
    # FastAPI / SQLAlchemy / httpx / asyncpg. Exporter chosen by env
    # var ``OTEL_EXPORTER`` (console / otlp / none). See DEPLOY.md
    # for the exporter swap recipe. Called LAST so the FastAPI
    # instrumentor sees the fully-configured route table.
    configure_tracing(app)

    return app


app = create_app()
