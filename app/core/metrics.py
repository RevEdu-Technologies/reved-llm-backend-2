"""Prometheus metrics for the RevEd backend.

Three things live here:

1. **Custom counters/histograms** that domain code can ``inc()`` or
   ``observe()``. Today: auth events and LLM token spend. These
   metrics are intentionally small in number; the dashboard derives
   most of its value from the per-endpoint request counter + latency
   histogram that ``prometheus-fastapi-instrumentator`` adds for us.

2. **Instrumentator setup** — wired in ``main.py`` via
   ``install_instrumentator(app)``. Exposes ``GET /metrics`` in
   Prometheus text format with the default request counter +
   latency histogram, scoped to the ``/api/v1/*`` routes (excludes
   ``/metrics`` itself + the docs).

3. **Helper functions** (``record_auth_event``, ``record_llm_tokens``)
   that wrap the ``.labels(...).inc(...)`` ceremony so callers stay
   readable. They never raise — Prometheus failures must not break
   the request path.

Convention: every metric is prefixed ``reved_`` so dashboards can grep
us out of a mixed scrape.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import Counter

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


# --- Custom metrics -------------------------------------------------------

AUTH_EVENTS = Counter(
    "reved_auth_events_total",
    "Authentication and authorization events.",
    labelnames=("event", "outcome", "reason"),
)

LLM_TOKENS = Counter(
    "reved_llm_tokens_total",
    "LLM tokens consumed, split by provider/model and prompt/completion.",
    labelnames=("provider", "model", "kind"),
)

CACHE_EVENTS = Counter(
    "reved_cache_events_total",
    "Cache hit / miss events from app.services.cache.cached_call.",
    labelnames=("namespace", "outcome"),
)


# --- Helpers --------------------------------------------------------------


def record_auth_event(
    *,
    event: str,
    outcome: str,
    reason: str | None = None,
) -> None:
    """Increment the auth-events counter. Never raises."""

    try:
        AUTH_EVENTS.labels(
            event=event or "unknown",
            outcome=outcome or "unknown",
            reason=reason or "none",
        ).inc()
    except Exception as exc:  # noqa: BLE001 - telemetry must not break callers
        logger.debug("metric_record_failure auth_events: %s", exc)


def record_cache_event(*, namespace: str, outcome: str) -> None:
    """Increment the cache hit / miss counter. Never raises.

    ``outcome`` is one of ``hit`` / ``miss``. Hit rate per namespace is
    derived in the dashboard as ``hit / (hit + miss)``.
    """

    try:
        CACHE_EVENTS.labels(
            namespace=namespace or "unknown",
            outcome=outcome or "unknown",
        ).inc()
    except Exception as exc:  # noqa: BLE001
        logger.debug("metric_record_failure cache_events: %s", exc)


def record_llm_tokens(
    *,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Record prompt/completion token counts for one LLM call. Never raises."""

    try:
        if prompt_tokens:
            LLM_TOKENS.labels(provider=provider, model=model, kind="prompt").inc(prompt_tokens)
        if completion_tokens:
            LLM_TOKENS.labels(
                provider=provider, model=model, kind="completion"
            ).inc(completion_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.debug("metric_record_failure llm_tokens: %s", exc)


# --- Instrumentator -------------------------------------------------------


def install_instrumentator(app: "FastAPI") -> None:
    """Attach prometheus-fastapi-instrumentator to the app and expose /metrics.

    Adds the default request counter + latency histogram, tagged by
    handler + method + status. We exclude ``/metrics`` itself and the
    interactive docs so they don't show up in p95 calculations.
    """

    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        logger.warning(
            "prometheus-fastapi-instrumentator not installed; /metrics endpoint "
            "will not be available. Install with `pip install prometheus-fastapi-instrumentator`."
        )
        return

    # Same exclude set as the access log — health probes, /metrics
    # itself, and the docs pages would skew p95 and burn cardinality.
    # See ``app.core.logging.OBSERVABILITY_EXCLUDED_PATHS`` for the
    # canonical list.
    from app.core.logging import OBSERVABILITY_EXCLUDED_PATHS

    instrumentator = Instrumentator(
        should_group_status_codes=False,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        excluded_handlers=sorted(OBSERVABILITY_EXCLUDED_PATHS),
    )
    instrumentator.instrument(app)
    # expose() registers GET /metrics on the app. include_in_schema=False
    # keeps it out of the OpenAPI surface so frontends don't accidentally
    # discover it as a "real" API.
    instrumentator.expose(app, endpoint="/metrics", include_in_schema=False)


__all__ = [
    "AUTH_EVENTS",
    "CACHE_EVENTS",
    "LLM_TOKENS",
    "install_instrumentator",
    "record_auth_event",
    "record_cache_event",
    "record_llm_tokens",
]
