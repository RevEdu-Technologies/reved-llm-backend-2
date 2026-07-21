"""Cursor pagination for list endpoints (Phase 5 / N9).

Cursors are opaque to clients — frontend code passes whatever string the
server returned in ``next_cursor`` back as ``?cursor=...`` to fetch the
next page. The wire format is intentionally not documented in the public
API: it's an implementation detail so we can evolve it later.

Internal format
---------------
``base64url(json({"c": "<iso8601>", "i": "<uuid>"}))``

Decoded, the cursor names the (created_at, id) tuple of the LAST row of
the previous page. The next page filter is:

    WHERE (created_at, id) < (cursor.c, cursor.i)
    ORDER BY created_at DESC, id DESC
    LIMIT N

``id`` is a tie-breaker for rows with identical ``created_at`` so
pagination stays deterministic. The row-value comparison is picked up
by the composite indexes added in N5 (``created_at`` is the trailing
column on every list endpoint's covering index).

Page-size convention
--------------------
Call sites fetch ``limit + 1`` rows. If the extra row came back, the
caller drops it and emits ``next_cursor`` derived from the last kept
row. Otherwise ``next_cursor`` is ``None``. This is one extra row per
page in the wire response — a fixed cost for a known "is there more?"
answer.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import Column, tuple_


@dataclass(frozen=True, slots=True)
class Cursor:
    """Decoded cursor — the last (created_at, id) of a page boundary."""

    created_at: datetime
    id: uuid.UUID


def encode_cursor(*, created_at: datetime, id: uuid.UUID) -> str:
    """Encode a (created_at, id) pair into an opaque base64url string."""

    payload = {"c": created_at.isoformat(), "i": str(id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Cursor:
    """Decode an opaque cursor string. Raises 400 on malformed input.

    We deliberately surface 400 rather than 404 here: the cursor came
    from the client (or was hand-crafted), so it's an input error.
    """

    try:
        # Pad back to a multiple of 4 — urlsafe_b64encode strips trailing '='.
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        return Cursor(
            created_at=datetime.fromisoformat(payload["c"]),
            id=uuid.UUID(payload["i"]),
        )
    except Exception as exc:  # noqa: BLE001 - any parse failure → 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed pagination cursor.",
        ) from exc


def apply_after(stmt, *, created_at_col: Column, id_col: Column, cursor: Cursor):
    """Append the row-value cursor predicate to a SELECT statement.

    Pre-condition: the statement is already ``ORDER BY created_at DESC,
    id DESC``. The same column references must be passed here so the
    planner can match the composite index.
    """

    return stmt.where(
        tuple_(created_at_col, id_col) < tuple_(cursor.created_at, cursor.id)
    )


# Page-size guardrails are duplicated across call sites; centralise.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def clamp_limit(limit: int | None) -> int:
    """Clamp the client-supplied ``limit`` into the supported window."""

    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(limit), MAX_PAGE_SIZE))


__all__ = [
    "Cursor",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "apply_after",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
]
