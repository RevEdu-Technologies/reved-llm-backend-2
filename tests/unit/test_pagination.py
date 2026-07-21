"""Unit tests for the cursor pagination helper (Phase 5 / N9)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api._pagination import (
    Cursor,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


def test_encode_then_decode_round_trips():
    created = datetime(2026, 5, 17, 23, 30, 0, tzinfo=timezone.utc)
    ident = uuid.uuid4()
    encoded = encode_cursor(created_at=created, id=ident)
    decoded = decode_cursor(encoded)
    assert decoded == Cursor(created_at=created, id=ident)


def test_encode_produces_url_safe_string():
    """Cursors travel as query params — must avoid `+`, `/`, `=` padding."""

    encoded = encode_cursor(created_at=datetime.now(timezone.utc), id=uuid.uuid4())
    assert "+" not in encoded
    assert "/" not in encoded
    assert not encoded.endswith("=")


def test_decode_handles_padding_stripped_by_encoder():
    encoded = encode_cursor(created_at=datetime.now(timezone.utc), id=uuid.uuid4())
    # Already stripped by encode_cursor; decode must repad internally.
    Cursor_obj = decode_cursor(encoded)  # should not raise
    assert isinstance(Cursor_obj, Cursor)


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!",
        "",
        "%%%",
        "Zm9v",  # valid b64 of "foo" — not JSON
        "eyJ4IjogMX0",  # JSON {"x":1} — wrong keys
    ],
)
def test_decode_invalid_cursor_raises_400(bad: str):
    with pytest.raises(HTTPException) as exc_info:
        decode_cursor(bad)
    assert exc_info.value.status_code == 400
    assert "cursor" in exc_info.value.detail.lower()


def test_decode_preserves_uuid_and_microseconds():
    """Tiebreaker UUID and sub-second precision must survive the round-trip."""

    created = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    ident = uuid.UUID("11111111-2222-3333-4444-555555555555")
    decoded = decode_cursor(encode_cursor(created_at=created, id=ident))
    assert decoded.created_at == created
    assert decoded.id == ident


def test_clamp_limit_defaults_when_none():
    assert clamp_limit(None) == DEFAULT_PAGE_SIZE


def test_clamp_limit_clamps_to_max():
    assert clamp_limit(10_000) == MAX_PAGE_SIZE


def test_clamp_limit_clamps_to_min_one():
    assert clamp_limit(0) == 1
    assert clamp_limit(-50) == 1


def test_clamp_limit_passes_through_in_range():
    assert clamp_limit(73) == 73


def test_cursors_for_different_inputs_differ():
    """Sanity check that the encoding is not collapsing distinct inputs."""

    a = encode_cursor(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), id=uuid.uuid4())
    b = encode_cursor(created_at=datetime(2026, 1, 2, tzinfo=timezone.utc), id=uuid.uuid4())
    assert a != b
