"""Conservative cleanup utilities for extracted textbook text.

Two layers:
  * `clean_extracted_text` — low-level normalization (kept from v1).
  * `strip_boilerplate`    — drop front-matter (copyright, TOC) and back-matter
                             (Index, Bibliography) so chunking doesn't waste
                             budget on non-instructional content.
"""

from __future__ import annotations

import re

_PAGE_NUMBER_ONLY = re.compile(r"(?m)^\s*\d+\s*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_THREE_PLUS_BREAKS = re.compile(r"\n{3,}")

# Lines with 8+ consecutive dots are almost certainly TOC entries (page-number
# dot leaders). Real prose virtually never produces this pattern.
_TOC_LINE = re.compile(r"\.{8,}")

# Long ISBN/copyright/license/credits lines that pollute the front of the book.
_FRONT_MATTER_PATTERNS = (
    re.compile(r"\bISBN[- ]?\d", re.IGNORECASE),
    re.compile(r"\ball rights reserved\b", re.IGNORECASE),
    re.compile(r"\bcopyright\b", re.IGNORECASE),
    re.compile(r"\bcreative commons\b", re.IGNORECASE),
    re.compile(r"\blicens(e|ed)\b", re.IGNORECASE),
    re.compile(r"\bopenstax\b", re.IGNORECASE),
    re.compile(r"\bfoundation\b", re.IGNORECASE),
)

# Heading-like patterns that mark the start of real content. We look for the
# FIRST occurrence of these that is not on a TOC line — that's where the body
# of the book begins.
_REAL_HEADING_PATTERNS = (
    re.compile(r"^\s*CHAPTER\s+\d+\b", re.IGNORECASE),
    re.compile(r"^\s*Chapter\s+\d+\b"),
    re.compile(r"^\s*\d+\.\d+\s+[A-Z][A-Za-z]"),  # "4.1 Force ..."
)

# Back-matter markers — once we see one of these on a near-bare line, drop
# everything from that point on.
_BACK_MATTER_HEADINGS = re.compile(
    r"^\s*(Index|Bibliography|References|Glossary|Appendix\s+[A-Z]?)\s*$",
    re.IGNORECASE,
)


def _join_wrapped_lines(block: str) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if not lines:
        return ""

    joined: list[str] = [lines[0]]
    for line in lines[1:]:
        previous = joined[-1]
        if previous.endswith("-") and previous[:-1].endswith(tuple("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")):
            joined[-1] = previous[:-1] + line
            continue

        if previous.endswith((".", "!", "?", ":", ";")):
            joined.append(line)
            continue

        joined[-1] = f"{previous} {line}"

    return "\n".join(joined)


def clean_extracted_text(text: str) -> str:
    """Apply conservative normalization while keeping paragraphs readable."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHARS.sub("", normalized)
    normalized = _PAGE_NUMBER_ONLY.sub("", normalized)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = _WHITESPACE_RUN.sub(" ", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)

    blocks = [block for block in re.split(r"\n\s*\n", normalized) if block.strip()]
    cleaned_blocks = [_join_wrapped_lines(block) for block in blocks]
    cleaned = "\n\n".join(block for block in cleaned_blocks if block.strip())
    cleaned = _THREE_PLUS_BREAKS.sub("\n\n", cleaned)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


def _is_front_matter_line(line: str) -> bool:
    """Lines that almost always indicate copyright/legal/license boilerplate."""
    return any(p.search(line) for p in _FRONT_MATTER_PATTERNS)


def strip_boilerplate(text: str) -> str:
    """Conservative line-level cleanup of non-content noise.

    What we strip:
      * Individual lines containing TOC dot-leaders (``....``) — these are
        almost always TOC entries, regardless of where they appear in the
        document.
      * Individual lines matching front-matter patterns (Copyright, ISBN,
        Creative Commons, license, etc.).
      * Everything after a *standalone* back-matter heading (Index,
        Bibliography, References, Glossary, Appendix) — but only if that
        heading sits in the back third of the document AND no real heading
        appears after it.

    What we DO NOT do:
      * Try to locate a single "body-start" anchor. Different textbooks have
        the TOC in the front, back, or both — anchoring is brittle.

    The downstream chunker further filters chunks with quality heuristics
    (step G in the roadmap), so we don't need to be aggressive here.
    """

    lines = text.split("\n")
    if not lines:
        return text

    n_lines = len(lines)
    back_third_start = int(n_lines * 0.66)

    # Find a back-matter cutoff only if it sits in the back third AND no
    # real heading appears after it (otherwise we might be looking at an
    # Index reference inside the body).
    cutoff: int | None = None
    for idx in range(back_third_start, n_lines):
        if _BACK_MATTER_HEADINGS.match(lines[idx]):
            tail = lines[idx + 1 :]
            if not any(_TOC_LINE.search(t) is None and any(p.search(t) for p in _REAL_HEADING_PATTERNS) for t in tail):
                cutoff = idx
                break

    body = lines[:cutoff] if cutoff is not None else lines

    kept: list[str] = []
    for line in body:
        if _TOC_LINE.search(line):
            continue
        if _is_front_matter_line(line):
            continue
        kept.append(line)

    out = "\n".join(kept).strip()
    return out + ("\n" if out else "")
