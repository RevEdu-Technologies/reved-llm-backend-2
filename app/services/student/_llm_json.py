"""Shared helpers for LLM calls that must return structured JSON.

Llama and Mixtral sometimes wrap JSON in markdown fences or append an
explanation. These helpers defensively strip the common patterns and parse
the payload with a clear error message on failure.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.errors import UpstreamError

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_json_response(raw: str) -> dict[str, Any]:
    """Parse an LLM response that is expected to be a JSON object."""

    if not raw or not raw.strip():
        raise UpstreamError("The AI returned an empty response.")

    cleaned = _FENCE_PATTERN.sub("", raw).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise UpstreamError("The AI response was not valid JSON.")

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise UpstreamError("The AI response could not be parsed as JSON.") from exc

    if not isinstance(parsed, dict):
        raise UpstreamError("The AI response was not a JSON object.")
    return parsed


__all__ = ["parse_json_response"]
