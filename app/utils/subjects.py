"""Canonical subject vocabulary + tolerant normalization.

Single source of truth for the RevEd subject taxonomy. Lives in ``utils``
(no service/schema imports) so both the Pydantic schemas and the service
layer can depend on it without circular imports.

Two entry points:

* :func:`normalize_subject` — best-effort match returning
  ``(canonical | None, confidence)``. Used where "no confident match"
  is a meaningful signal (e.g. the tutor preflight falls through to an
  LLM pass).
* :func:`coerce_subject` — always returns *a* canonical value (falling
  back to ``"general"``). Used by request schemas so a free-text subject
  from the frontend (``"Science"``, ``"Mathematics"``, a raw
  ``classes.subject`` string) never produces a ``422`` — it degrades to
  ``general`` retrieval instead.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Literal

CanonicalSubject = Literal[
    "biology",
    "chemistry",
    "physics",
    "mathematics",
    "further_mathematics",
    "english_language",
    "literature_in_english",
    "economics",
    "government",
    "civic_education",
    "commerce",
    "accounting",
    "office_practice",
    "computer",
    "history",
    "religious_studies",
    "hausa",
    "igbo",
    "yoruba",
    "general",
]

CANONICAL_SUBJECTS: tuple[CanonicalSubject, ...] = (
    "biology",
    "chemistry",
    "physics",
    "mathematics",
    "further_mathematics",
    "english_language",
    "literature_in_english",
    "economics",
    "government",
    "civic_education",
    "commerce",
    "accounting",
    "office_practice",
    "computer",
    "history",
    "religious_studies",
    "hausa",
    "igbo",
    "yoruba",
    "general",
)

# Set form for cheap membership checks in schema validators.
VALID_SUBJECTS: set[str] = set(CANONICAL_SUBJECTS)

# Hand-curated aliases. Lowercased. Common short forms, the human-readable
# forms with spaces (e.g. "english language"), and the broad labels the
# RevEd frontend actually sends (e.g. "science", "social studies") all map
# to a snake_case canonical value.
_ALIASES: dict[str, CanonicalSubject] = {
    # Sciences
    "bio": "biology",
    "biol": "biology",
    "life science": "biology",
    "life sciences": "biology",
    "chem": "chemistry",
    "chemi": "chemistry",
    "phys": "physics",
    "physic": "physics",
    # Broad/umbrella labels the frontend's free-text subject field emits.
    # These aren't 1:1 with a single canonical subject, so they map to
    # "general" — retrieval stays unfiltered rather than rejecting the
    # request.
    "science": "general",
    "sciences": "general",
    "basic science": "general",
    "basic technology": "general",
    "social studies": "general",
    "general studies": "general",
    "vocational studies": "general",
    "pre-vocational studies": "general",
    "agricultural science": "general",
    "agriculture": "general",
    "physical education": "general",
    "health education": "general",
    # Mathematics
    "math": "mathematics",
    "maths": "mathematics",
    "general math": "mathematics",
    "general mathematics": "mathematics",
    "further math": "further_mathematics",
    "further maths": "further_mathematics",
    "further mathematics": "further_mathematics",
    # English / Literature
    "english": "english_language",
    "english language": "english_language",
    "lang": "english_language",
    "literature": "literature_in_english",
    "literature in english": "literature_in_english",
    "lit": "literature_in_english",
    # Social sciences
    "econ": "economics",
    "economics": "economics",
    "gov": "government",
    "government": "government",
    "civic": "civic_education",
    "civics": "civic_education",
    "civic education": "civic_education",
    # Business / vocational
    "commerce": "commerce",
    "comm": "commerce",
    "accounting": "accounting",
    "accounts": "accounting",
    "financial accounts": "accounting",
    "office practice": "office_practice",
    "computer": "computer",
    "computer science": "computer",
    "computing": "computer",
    "ict": "computer",
    "information and communication technology": "computer",
    # Humanities
    "history": "history",
    "hist": "history",
    "religious studies": "religious_studies",
    "crs": "religious_studies",
    "christian religious studies": "religious_studies",
    "irs": "religious_studies",
    "islamic religious studies": "religious_studies",
    # Nigerian languages
    "hausa": "hausa",
    "igbo": "igbo",
    "yoruba": "yoruba",
}

# Similarity threshold for accepting a fuzzy match. Tuned so:
#   "biologi" -> biology (0.857)  accepted
#   "chemstry" -> chemistry (0.941) accepted
#   "math" -> no match (best ~0.44) rejected -> alias/LLM fallback
_MIN_SIMILARITY = 0.72


def normalize_subject(raw: str | None) -> tuple[CanonicalSubject | None, float]:
    """Return the canonical subject for ``raw`` if confidently matched.

    Resolution order:
      1. Exact (case-insensitive) match against canonical set.
      2. Exact match against the curated alias map.
      3. Fuzzy ratio against each canonical subject, accept if >= threshold.

    The second element is the confidence in [0, 1]. ``(None, 0.0)`` means
    no confident match — callers decide whether to fall back.
    """

    if not raw:
        return None, 0.0

    normalized = raw.strip().lower()
    if not normalized:
        return None, 0.0

    if normalized in VALID_SUBJECTS:
        return normalized, 1.0  # type: ignore[return-value]

    if normalized in _ALIASES:
        return _ALIASES[normalized], 1.0

    best_subject: CanonicalSubject | None = None
    best_score = 0.0
    for candidate in CANONICAL_SUBJECTS:
        score = SequenceMatcher(None, normalized, candidate).ratio()
        if score > best_score:
            best_score = score
            best_subject = candidate

    if best_subject is not None and best_score >= _MIN_SIMILARITY:
        return best_subject, best_score

    return None, best_score


def coerce_subject(raw: str | None, *, default: CanonicalSubject = "general") -> CanonicalSubject:
    """Always return a canonical subject — never raise.

    Used by request schemas so a free-text or umbrella subject from the
    frontend degrades to ``default`` ("general") instead of a 422. When
    ``raw`` is empty, returns ``default``.
    """

    match, _score = normalize_subject(raw)
    return match if match is not None else default


__all__ = [
    "CANONICAL_SUBJECTS",
    "CanonicalSubject",
    "VALID_SUBJECTS",
    "coerce_subject",
    "normalize_subject",
]
