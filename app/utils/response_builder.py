"""Helpers to build the standard RevEd API response envelope.

Every route must return one of these helpers so the frontend receives a
consistent ``{"status", "data", "message", "role"}`` shape regardless of which
service or user role handled the request.
"""

from __future__ import annotations

from typing import Any

from app.schemas.common import APIResponse, ErrorData, UserRole


def success_response(
    *,
    role: UserRole,
    data: Any = None,
    message: str = "",
) -> APIResponse[Any]:
    """Build a success envelope for the given role."""

    return APIResponse[Any](
        status="success",
        data=data,
        message=message,
        role=role,
    )


def error_response(
    *,
    role: UserRole,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> APIResponse[ErrorData]:
    """Build an error envelope for the given role."""

    return APIResponse[ErrorData](
        status="error",
        data=ErrorData(code=code, details=details),
        message=message,
        role=role,
    )
