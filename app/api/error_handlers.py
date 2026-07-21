"""Centralized FastAPI exception handlers that emit the RevEd envelope."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import ConfigurationError
from app.core.errors import RevEdError
from app.core.i18n import negotiate_language, translate
from app.llm.client import LLMClientError
from app.schemas.common import UserRole
from app.utils.response_builder import error_response

logger = logging.getLogger(__name__)


def _role_from_path(path: str) -> UserRole:
    segments = path.lower().split("/")
    for role in ("student", "teacher", "parent", "admin"):
        if role in segments:
            return role  # type: ignore[return-value]
    return "system"


def _lang(request: Request) -> str:
    """Negotiate the response language from the request's Accept-Language.

    Read straight off the request header rather than a contextvar so that
    *every* handler localizes correctly — including the catch-all 500
    handler, which runs in ServerErrorMiddleware (outside the per-request
    LanguageMiddleware contextvar scope).
    """

    return negotiate_language(request.headers.get("accept-language"))


def _json(envelope, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


def _sanitize_validation_errors(errors: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return JSON-safe versions of pydantic/FastAPI validation errors."""

    sanitized: list[dict[str, object]] = []
    for err in errors:
        safe: dict[str, object] = {}
        for key, value in err.items():
            if key == "ctx" and isinstance(value, dict):
                safe[key] = {k: str(v) for k, v in value.items()}
            else:
                safe[key] = value
        sanitized.append(safe)
    return sanitized


def register_error_handlers(app: FastAPI) -> None:
    """Attach RevEd error handlers to a FastAPI application."""

    @app.exception_handler(RevEdError)
    async def _handle_reved_error(request: Request, exc: RevEdError) -> JSONResponse:
        logger.warning("Domain error on %s: %s", request.url.path, exc)
        # Domain errors usually carry a specific, context-rich English
        # message from the service call site (e.g. "Goal 123 not found"),
        # which we can't translate after the fact — keep it. When raised
        # without a message, fall back to a localized, code-derived string.
        custom = str(exc)
        message = custom or translate(
            f"error.{exc.code}", _lang(request), default="Request could not be processed."
        )
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code=exc.code,
            message=message,
        )
        return _json(envelope, exc.http_status)

    @app.exception_handler(ConfigurationError)
    async def _handle_config_error(request: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("Configuration error on %s: %s", request.url.path, exc)
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code="configuration_error",
            message=translate("error.configuration", _lang(request)),
        )
        return _json(envelope, 503)

    @app.exception_handler(LLMClientError)
    async def _handle_llm_error(request: Request, exc: LLMClientError) -> JSONResponse:
        logger.error("LLM upstream error on %s: %s", request.url.path, exc)
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code="upstream_error",
            message=translate("error.upstream_error", _lang(request)),
        )
        return _json(envelope, 503)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code="validation_error",
            message=translate("error.validation", _lang(request)),
            details={"errors": _sanitize_validation_errors(exc.errors())},
        )
        return _json(envelope, 422)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        lang = _lang(request)
        # 404s are the common case (unrouted path / missing resource) and
        # carry a generic English "Not Found" from Starlette — localize it.
        # Other codes may carry a specific detail worth surfacing verbatim.
        if exc.status_code == 404:
            message = translate("error.not_found", lang)
        elif exc.detail:
            message = str(exc.detail)
        else:
            message = translate("error.http_generic", lang)
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code="http_error",
            message=message,
        )
        return _json(envelope, exc.status_code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        envelope = error_response(
            role=_role_from_path(request.url.path),
            code="internal_error",
            message=translate("error.internal", _lang(request)),
        )
        return _json(envelope, 500)
