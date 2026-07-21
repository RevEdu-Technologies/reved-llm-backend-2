"""Unit tests for the deterministic subject normalizer."""

from __future__ import annotations

from app.services.student._subject_matcher import normalize_subject


def test_exact_canonical_match():
    subject, confidence = normalize_subject("biology")
    assert subject == "biology"
    assert confidence == 1.0


def test_case_insensitive_match():
    subject, _ = normalize_subject("PHYSICS")
    assert subject == "physics"


def test_alias_short_form():
    assert normalize_subject("bio")[0] == "biology"
    assert normalize_subject("chem")[0] == "chemistry"
    assert normalize_subject("phys")[0] == "physics"


def test_alias_life_science():
    assert normalize_subject("life science")[0] == "biology"
    assert normalize_subject("Life Sciences")[0] == "biology"


def test_fuzzy_typo_chemistry():
    subject, confidence = normalize_subject("chemstry")
    assert subject == "chemistry"
    assert confidence >= 0.72


def test_fuzzy_typo_biology():
    subject, _ = normalize_subject("biologi")
    assert subject == "biology"


def test_unrelated_rejected():
    # An input that is not a curriculum subject and is far enough from any
    # canonical name that fuzzy matching declines (confidence < 0.72).
    # ``mathematics`` used to be the example here but is now a canonical
    # subject; ``underwater basket weaving`` is the same idea kept
    # obviously-not-a-school-subject.
    subject, _ = normalize_subject("underwater basket weaving")
    assert subject is None


def test_none_input():
    assert normalize_subject(None) == (None, 0.0)


def test_empty_string():
    assert normalize_subject("")[0] is None
    assert normalize_subject("   ")[0] is None


def test_whitespace_trimmed():
    assert normalize_subject("  biology  ")[0] == "biology"
