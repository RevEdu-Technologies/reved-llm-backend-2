"""Lightweight internationalization for user-facing API strings.

Scope and design
----------------
The RevEd API surfaces a small, fixed set of user-facing strings — the
``message`` field of the error envelope (validation, not-found, rate-limit,
upstream, configuration, internal) plus a handful of generic phrases. That
is far too small to justify a full ``babel`` / gettext ``.po`` toolchain
(message extraction, compilation, locale dirs). Instead we keep an in-tree
catalog keyed by a stable message id, negotiate the language from the
request's ``Accept-Language`` header, and format with ``str.format`` params.

Public surface
--------------
* :data:`SUPPORTED_LANGUAGES` / :data:`DEFAULT_LANGUAGE`
* :func:`negotiate_language` — parse ``Accept-Language`` → a supported tag.
* :func:`translate` — message id + language → localized string.
* :data:`current_language` contextvar + :func:`set_current_language` /
  :func:`get_current_language` — set by ``LanguageMiddleware`` so service
  code can localize without threading the request through every call.

Adding a language is a single column in :data:`MESSAGES` plus an entry in
:data:`SUPPORTED_LANGUAGES`. Adding a string is one new catalog row.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

DEFAULT_LANGUAGE = "en"
"""Language used when negotiation finds no supported match."""

# Order is not significant; membership is what negotiation checks.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"en", "fr"})
"""Primary-subtag language codes the catalog covers."""


# Catalog: message-id → {language → template}. Templates may carry
# ``{named}`` ``str.format`` placeholders; callers pass them as kwargs to
# :func:`translate`. Every id MUST have an ``en`` entry (the ultimate
# fallback). Keep ids namespaced (``error.*``) so future non-error strings
# don't collide.
MESSAGES: dict[str, dict[str, str]] = {
    "error.validation": {
        "en": "Your request contains invalid data.",
        "fr": "Votre requête contient des données invalides.",
    },
    "error.not_found": {
        "en": "The requested resource was not found.",
        "fr": "La ressource demandée est introuvable.",
    },
    "error.role_violation": {
        "en": "This action is outside your role's permissions.",
        "fr": "Cette action dépasse les autorisations de votre rôle.",
    },
    "error.upstream_error": {
        "en": "The AI service is temporarily unavailable. Please try again later.",
        "fr": "Le service d'IA est temporairement indisponible. Veuillez réessayer plus tard.",
    },
    "error.configuration": {
        "en": "The service is temporarily misconfigured. Please try again later.",
        "fr": "Le service est temporairement mal configuré. Veuillez réessayer plus tard.",
    },
    "error.rate_limited": {
        "en": "Rate limit exceeded ({detail}). Please retry shortly.",
        "fr": "Limite de requêtes dépassée ({detail}). Veuillez réessayer dans un instant.",
    },
    "error.http_generic": {
        "en": "Request failed.",
        "fr": "La requête a échoué.",
    },
    "error.processing": {
        "en": "Request could not be processed.",
        "fr": "La requête n'a pas pu être traitée.",
    },
    "error.internal": {
        "en": "An unexpected error occurred. Please try again.",
        "fr": "Une erreur inattendue s'est produite. Veuillez réessayer.",
    },
}


current_language: contextvars.ContextVar[str] = contextvars.ContextVar(
    "reved_current_language", default=DEFAULT_LANGUAGE
)
"""Per-request negotiated language. Set by ``LanguageMiddleware`` before the
handler runs so any code in the request task can call :func:`translate`
without access to the request object."""


def set_current_language(language: str) -> contextvars.Token:
    """Bind the request's language; returns a reset token for teardown."""

    return current_language.set(language or DEFAULT_LANGUAGE)


def get_current_language() -> str:
    """Return the language bound for the current request (or the default)."""

    return current_language.get()


def negotiate_language(accept_language: str | None) -> str:
    """Pick the best supported language from an ``Accept-Language`` header.

    Parses the standard quality-weighted list (``fr-FR,fr;q=0.9,en;q=0.8``),
    compares on the primary subtag (``fr-FR`` → ``fr``), and returns the
    highest-q supported tag. Falls back to :data:`DEFAULT_LANGUAGE` for a
    missing/empty header, a wildcard, or no supported match. Never raises.
    """

    if not accept_language or not accept_language.strip():
        return DEFAULT_LANGUAGE

    weighted: list[tuple[float, int, str]] = []
    for index, part in enumerate(accept_language.split(",")):
        part = part.strip()
        if not part:
            continue
        token, _, params = part.partition(";")
        primary = token.strip().lower().split("-", 1)[0]
        if not primary:
            continue
        quality = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    quality = float(param[2:])
                except ValueError:
                    quality = 0.0
        # index keeps the original order as a stable tie-breaker so equal-q
        # languages are tried left-to-right (header preference order).
        weighted.append((quality, index, primary))

    for _quality, _index, primary in sorted(weighted, key=lambda w: (-w[0], w[1])):
        if primary in SUPPORTED_LANGUAGES:
            return primary
    return DEFAULT_LANGUAGE


def translate(
    message_id: str,
    language: str | None = None,
    *,
    default: str | None = None,
    **params: object,
) -> str:
    """Return the localized string for ``message_id`` in ``language``.

    Resolution: requested language → English → ``default`` → the raw
    ``message_id``. ``params`` are applied with ``str.format``; a formatting
    error (missing placeholder) degrades to the unformatted template rather
    than raising, so a catalog typo can never 500 a request.
    """

    language = language or get_current_language()
    entry = MESSAGES.get(message_id)
    if entry is None:
        text = default if default is not None else message_id
    else:
        text = entry.get(language) or entry.get(DEFAULT_LANGUAGE) or default or message_id

    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


class LanguageMiddleware(BaseHTTPMiddleware):
    """Bind the negotiated language to the contextvar for the request.

    Lets service-layer code call :func:`translate` (via
    :func:`get_current_language`) without threading the request object
    through every signature. The error handlers deliberately do *not* rely
    on this — they read ``Accept-Language`` off the request directly so that
    the catch-all 500 handler (which runs outside this middleware's scope in
    Starlette's ServerErrorMiddleware) still localizes correctly.
    """

    def __init__(self, app: "ASGIApp") -> None:
        super().__init__(app)

    async def dispatch(self, request: "Request", call_next) -> "Response":
        token = set_current_language(
            negotiate_language(request.headers.get("accept-language"))
        )
        try:
            return await call_next(request)
        finally:
            current_language.reset(token)


__all__ = [
    "DEFAULT_LANGUAGE",
    "MESSAGES",
    "SUPPORTED_LANGUAGES",
    "LanguageMiddleware",
    "current_language",
    "get_current_language",
    "negotiate_language",
    "set_current_language",
    "translate",
]
