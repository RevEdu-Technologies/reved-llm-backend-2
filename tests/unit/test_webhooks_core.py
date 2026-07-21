"""Unit tests for the pure webhook helpers — signing, verification, backoff."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.core.webhooks import (
    DEFAULT_MAX_ATTEMPTS,
    backoff_seconds,
    is_success_status,
    secret_token,
    sign_payload,
    verify_signature,
)


def test_sign_payload_matches_manual_hmac():
    secret = "s3cr3t"
    body = b'{"event":"x"}'
    expected = "hmac-sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sign_payload(secret, body) == expected


def test_verify_signature_round_trip():
    secret = secret_token()
    body = b'{"hello":"world"}'
    sig = sign_payload(secret, body)
    assert verify_signature(secret, body, sig) is True
    # Without the prefix is also accepted.
    assert verify_signature(secret, body, sig.split("=", 1)[1]) is True


def test_verify_signature_rejects_tampering():
    secret = secret_token()
    body = b'{"amount":100}'
    sig = sign_payload(secret, body)
    assert verify_signature(secret, b'{"amount":999}', sig) is False
    assert verify_signature("wrong-secret", body, sig) is False


@pytest.mark.parametrize("bad", [None, "", "garbage", "hmac-sha256=", "sha1=abc"])
def test_verify_signature_handles_missing_or_malformed(bad):
    assert verify_signature("secret", b"body", bad) is False


def test_secret_token_is_unique_and_urlsafe():
    a, b = secret_token(), secret_token()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_backoff_is_monotonic_and_capped():
    delays = [backoff_seconds(n, base=10, cap=3600) for n in range(1, 12)]
    # Non-decreasing.
    assert delays == sorted(delays)
    # Doubles early: 10, 20, 40, ...
    assert delays[0] == 10
    assert delays[1] == 20
    assert delays[2] == 40
    # Capped at the ceiling for large attempt counts.
    assert delays[-1] == 3600


def test_backoff_zero_or_negative_attempts_returns_base():
    assert backoff_seconds(0, base=10) == 10
    assert backoff_seconds(-3, base=10) == 10


@pytest.mark.parametrize(
    "code,ok",
    [(200, True), (201, True), (204, True), (299, True), (300, False), (404, False), (500, False)],
)
def test_is_success_status(code, ok):
    assert is_success_status(code) is ok


def test_default_max_attempts_is_sane():
    assert DEFAULT_MAX_ATTEMPTS >= 1
