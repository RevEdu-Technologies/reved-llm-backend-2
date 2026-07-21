"""End-to-end: error envelopes localize their ``message`` via Accept-Language.

These exercise the real app + error handlers with no DB / LLM dependency:
a malformed body triggers ``RequestValidationError`` (422) and an unrouted
path triggers a 404 — both before any service call. We assert the ``message``
field flips to French when the client asks for it, and stays English
otherwise, while ``code`` (used for branching) is locale-independent.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.i18n import MESSAGES
from main import app

client = TestClient(app)


def test_validation_error_message_is_english_by_default():
    resp = client.post("/api/v1/student/ask", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["data"]["code"] == "validation_error"
    assert body["message"] == MESSAGES["error.validation"]["en"]


def test_validation_error_message_is_translated_for_accept_language_fr():
    resp = client.post(
        "/api/v1/student/ask",
        json={},
        headers={"Accept-Language": "fr"},
    )
    assert resp.status_code == 422
    body = resp.json()
    # code stays stable across locales; only the human message changes.
    assert body["data"]["code"] == "validation_error"
    assert body["message"] == MESSAGES["error.validation"]["fr"]


def test_validation_error_honours_quality_weighted_accept_language():
    resp = client.post(
        "/api/v1/student/ask",
        json={},
        headers={"Accept-Language": "en;q=0.4, fr;q=0.9"},
    )
    assert resp.json()["message"] == MESSAGES["error.validation"]["fr"]


def test_unsupported_language_falls_back_to_english():
    resp = client.post(
        "/api/v1/student/ask",
        json={},
        headers={"Accept-Language": "de-DE,de;q=0.9"},
    )
    assert resp.json()["message"] == MESSAGES["error.validation"]["en"]


def test_not_found_message_is_translated():
    en = client.get("/api/v1/this-route-does-not-exist")
    assert en.status_code == 404
    assert en.json()["message"] == MESSAGES["error.not_found"]["en"]

    fr = client.get(
        "/api/v1/this-route-does-not-exist",
        headers={"Accept-Language": "fr-FR,fr;q=0.9"},
    )
    assert fr.status_code == 404
    assert fr.json()["message"] == MESSAGES["error.not_found"]["fr"]
