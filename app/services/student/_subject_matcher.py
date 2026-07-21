"""Tier-1 deterministic subject normalizer.

Handles typos and short forms against the full canonical subject set
without calling an LLM. Returns ``(subject, confidence)`` where subject is
``None`` if no confident match was found — the caller then falls through
to the LLM preflight pass.

The canonical taxonomy + alias table now live in
:mod:`app.utils.subjects` (a dependency-free module) so the Pydantic
schemas can share the exact same normalization without importing the
service layer. This module re-exports the public names so existing
imports (tutor service, preflight) keep working.
"""

from __future__ import annotations

from app.utils.subjects import (
    CANONICAL_SUBJECTS,
    CanonicalSubject,
    normalize_subject,
)

__all__ = ["CANONICAL_SUBJECTS", "CanonicalSubject", "normalize_subject"]
