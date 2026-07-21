"""JSON logging + per-request correlation IDs.

Two things ship together here because they pair so cleanly:

1. ``configure_logging()`` replaces the human-friendly
   ``logging.basicConfig`` call in ``main.py`` with a JSON formatter
   (python-json-logger). Every line on stdout becomes a single JSON
   object that a log collector can ingest without regex parsing.

2. ``RequestIdMiddleware`` mints a UUID per request, stashes it in a
   ``contextvars.ContextVar`` so every log record made during that
   request carries the same ``request_id`` field, and echoes it back on
   the response as ``X-Request-Id``. Callers can supply their own
   ``X-Request-Id`` header (e.g. an ingress gateway) and we honor it,
   which lets a single trace span ingress → app → log aggregator.

The ``reved.audit`` logger is *not* touched here — it already emits its
own JSON via a dedicated handler. We disable its propagation in
``app/core/audit.py`` so audit records aren't double-emitted.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid
from typing import Any

from pythonjsonlogger.json import JsonFormatter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.security import ANONYMOUS_ROLE, ANONYMOUS_USER_ID
from app.core.tracing import current_span_id, current_trace_id

_REQUEST_ID_HEADER = "X-Request-Id"

# Single source of truth for "endpoints that should not contribute to
# observability." Health probes fire every few seconds from k8s/uvicorn;
# /metrics is scraped by Prometheus on a tight loop; the docs paths are
# only ever hit interactively. Both the access log (this module) and
# the Prometheus instrumentator (app.core.metrics) consume this set —
# adding a new probe endpoint takes one edit, not two.
OBSERVABILITY_EXCLUDED_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/health/ready",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
    }
)

_access_logger = logging.getLogger("reved.access")

_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reved_request_id", default=None
)


def current_request_id() -> str | None:
    """Return the request id bound to the current async task, if any."""

    return _request_id_ctx.get()


class _CorrelationFilter(logging.Filter):
    """Inject request/trace/span IDs into every log record.

    A single filter covers all three contextvars — one pass per record,
    one place to extend when N2 adds OpenTelemetry. Records made
    outside an HTTP request leave each field as ``None`` and the
    formatter strips them.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.request_id = _request_id_ctx.get()
        record.trace_id = current_trace_id()
        record.span_id = current_span_id()
        return True


# Back-compat alias: external code/tests may still reference
# ``_RequestIdFilter``. New code should use ``_CorrelationFilter``.
_RequestIdFilter = _CorrelationFilter


class _RevEdJsonFormatter(JsonFormatter):
    """JsonFormatter that always surfaces the correlation IDs + a timestamp."""

    # Fields the filter writes; emitted only when truthy so records made
    # outside a request stay tidy.
    _CORRELATION_FIELDS = ("request_id", "trace_id", "span_id")

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        # python-json-logger 3.x pre-populates keys named in the format
        # string from record.__dict__, which gives us None for fields we
        # synthesize here. Overwrite unconditionally so they always carry
        # a value.
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["timestamp"] = self.formatTime(record, self.datefmt)
        for field in self._CORRELATION_FIELDS:
            value = getattr(record, field, None)
            if value:
                log_record[field] = value
            else:
                log_record.pop(field, None)


def configure_logging(level: int = logging.INFO) -> None:
    """Install a JSON formatter on the root logger.

    Safe to call multiple times — subsequent calls replace the handlers
    on the root logger rather than stacking them. The dedicated
    ``reved.audit`` logger keeps its own handler (``propagate=False``)
    so its output is unaffected.
    """

    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        _RevEdJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(_CorrelationFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn's access log is noisy and duplicates our request-id middleware;
    # downgrade it so only warnings come through. Uvicorn's error log keeps
    # the default level so startup failures still surface.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind a per-request UUID into a contextvar and the response."""

    def __init__(self, app: ASGIApp, header_name: str = _REQUEST_ID_HEADER) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(self.header_name, "").strip()
        request_id = incoming or uuid.uuid4().hex
        token = _request_id_ctx.set(request_id)
        try:
            response: Response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)
        response.headers[self.header_name] = request_id
        return response


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured access-log line per request.

    The line carries ``method``, ``path``, ``status``, ``duration_ms``,
    ``user_id``, ``role``, and ``request_id`` so an operator can
    correlate latency, errors, and identity from a single source.

    Identity is read from ``request.state.auth_user`` — set by
    ``app.core.security.get_current_user`` whenever a route declares an
    auth dependency. Routes excluded from observability (health,
    /metrics, docs — see ``OBSERVABILITY_EXCLUDED_PATHS``) produce no
    line at all; everywhere else, requests whose auth dependency didn't
    run (404 on an unrouted path, 401 short-circuit) log
    ``user_id=role=anonymous``.

    Errors raised inside the handler still produce one line: the
    surrounding try/finally ensures ``duration_ms`` and the resulting
    ``status`` (5xx from FastAPI's exception handlers) are captured.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.scope["path"]
        if path in OBSERVABILITY_EXCLUDED_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            # Skip the dict + str() + getattr work when the access
            # logger is disabled (e.g. tests that pin to WARNING). Still
            # pay one perf_counter() diff so the cost is constant.
            if _access_logger.isEnabledFor(logging.INFO):
                duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
                user = getattr(request.state, "auth_user", None)
                _access_logger.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": path,
                        "status": status_code,
                        "duration_ms": duration_ms,
                        "user_id": str(user.user_id) if user else ANONYMOUS_USER_ID,
                        "role": user.role if user else ANONYMOUS_ROLE,
                        # Correlation IDs pulled explicitly from the
                        # contextvars (vs. relying on _CorrelationFilter
                        # on the root handler) so the line carries them
                        # even when the access logger is configured
                        # with ``propagate=False``.
                        "request_id": current_request_id(),
                        "trace_id": current_trace_id(),
                        "span_id": current_span_id(),
                    },
                )


__all__ = [
    "OBSERVABILITY_EXCLUDED_PATHS",
    "RequestIdMiddleware",
    "RequestLogMiddleware",
    "configure_logging",
    "current_request_id",
]
