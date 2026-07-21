"""Unit tests for the i18n catalog, language negotiation, and translation."""

from __future__ import annotations

import pytest

from app.core.i18n import (
    DEFAULT_LANGUAGE,
    MESSAGES,
    SUPPORTED_LANGUAGES,
    negotiate_language,
    translate,
)


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, "en"),
        ("", "en"),
        ("   ", "en"),
        ("fr", "fr"),
        ("fr-FR", "fr"),
        ("fr-FR,fr;q=0.9,en;q=0.8", "fr"),
        ("en-US,en;q=0.9", "en"),
        ("de", "en"),  # unsupported → default
        ("de,es;q=0.9", "en"),  # all unsupported → default
        ("*", "en"),  # wildcard → default
        ("en;q=0.3, fr;q=0.9", "fr"),  # highest q wins regardless of order
        ("fr;q=0.8, en;q=0.8", "fr"),  # equal q → header order tie-break
        ("xx, fr", "fr"),  # skip unsupported, take next supported
    ],
)
def test_negotiate_language(header, expected):
    assert negotiate_language(header) == expected


def test_negotiate_language_handles_malformed_q():
    # A non-numeric q must not raise; it just deprioritizes that tag.
    assert negotiate_language("fr;q=notanumber, en;q=0.5") == "en"


def test_translate_known_key_each_language():
    assert translate("error.validation", "en") == MESSAGES["error.validation"]["en"]
    assert translate("error.validation", "fr") == MESSAGES["error.validation"]["fr"]


def test_translate_falls_back_to_english_for_unsupported_language():
    # A language with no catalog column degrades to English, never to the id.
    assert translate("error.validation", "de") == MESSAGES["error.validation"]["en"]


def test_translate_unknown_key_uses_default_then_id():
    assert translate("error.does_not_exist", "fr", default="fallback") == "fallback"
    assert translate("error.does_not_exist", "fr") == "error.does_not_exist"


def test_translate_applies_format_params():
    out = translate("error.rate_limited", "en", detail="10 per minute")
    assert "10 per minute" in out


def test_translate_missing_param_does_not_raise():
    # Template wants {detail}; omitting it returns the unformatted template.
    out = translate("error.rate_limited", "en")
    assert "{detail}" in out  # degraded, but no exception


def test_every_catalog_entry_has_english_and_all_supported_languages():
    for message_id, entry in MESSAGES.items():
        assert DEFAULT_LANGUAGE in entry, f"{message_id} missing English"
        for lang in SUPPORTED_LANGUAGES:
            assert lang in entry, f"{message_id} missing {lang}"
            assert entry[lang].strip(), f"{message_id}/{lang} is empty"
