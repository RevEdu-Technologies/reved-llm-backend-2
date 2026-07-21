"""Domain-level exception hierarchy used by the service layer.

These exceptions let service code signal specific failure modes (validation,
not-found, role denial, upstream failure) without coupling to FastAPI.
The API layer translates them into HTTP responses via a centralized handler.
"""

from __future__ import annotations


class RevEdError(RuntimeError):
    """Base class for all application-level RevEd errors."""

    code: str = "reved_error"
    http_status: int = 500


class ValidationError(RevEdError):
    """Raised when user input is semantically invalid at the service layer."""

    code = "validation_error"
    http_status = 422


class NotFoundError(RevEdError):
    """Raised when a requested resource does not exist."""

    code = "not_found"
    http_status = 404


class RoleViolationError(RevEdError):
    """Raised when a query or action violates the caller's role scope."""

    code = "role_violation"
    http_status = 403


class UpstreamError(RevEdError):
    """Raised when an external dependency (LLM, vector store) fails."""

    code = "upstream_error"
    http_status = 503


__all__ = [
    "NotFoundError",
    "RevEdError",
    "RoleViolationError",
    "UpstreamError",
    "ValidationError",
]
