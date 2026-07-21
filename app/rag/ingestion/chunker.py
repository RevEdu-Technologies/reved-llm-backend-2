"""Structure-aware, token-based chunker for cleaned textbook text.

Design summary:

* Headings (chapter / section) split the stream. A chunk is never allowed to
  cross a heading boundary.
* Equation-dense lines (lots of symbols, `=` present, short) and Markdown-ish
  code blocks are treated as atomic units that cannot be split mid-equation.
* Paragraphs are the default unit.
* Token budgeting uses ``tiktoken`` (cl100k_base) so we control prompt-side
  costs precisely. Char counts are still recorded for diagnostics.
* Each chunk records its nearest ``chapter`` and ``section`` headings; that
  feeds the step-B metadata enrichment (added in a later commit).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from .classifier import (
    QUALITY_DROP_THRESHOLD,
    classify_chunk,
    visibility_for_content_type,
)

LOGGER = logging.getLogger(__name__)


# Heading detection — these patterns identify the START of a new logical
# section. Lines that match these begin a fresh chunk. A line containing TOC
# dot leaders is rejected (the preprocessor should already have dropped these,
# but the chunker is defensive).
_CHAPTER_RE = re.compile(r"^\s*(?:CHAPTER|Chapter)\s+(\d+)\b\s*(.*)")
_SECTION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.\d+)?\s+([A-Z][A-Za-z].*)")
_TOC_DOTS = re.compile(r"\.{8,}")

# Equation-heuristic: short-ish line with at least one `=` and a high ratio of
# non-alphabetic characters. We refuse to split paragraphs that contain such
# lines, to avoid splitting mid-derivation.
_EQUATION_INLINE = re.compile(r"[=≈≠≤≥±√∑∫π].*[+\-*/]|[A-Za-z]\s*=\s*[^=]")

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'`])")


@dataclass(slots=True)
class ChunkingConfig:
    """Token-budget configuration."""

    target_tokens: int = 450
    overlap_tokens: int = 80
    min_chunk_tokens: int = 150
    hard_max_tokens: int = 700
    # Encoder name passed to tiktoken. cl100k_base matches the GPT-4/3.5 family
    # and is a stable open encoding; bge tokenizer differs slightly but token
    # counts are close enough for budgeting.
    encoding_name: str = "cl100k_base"

    def __post_init__(self) -> None:
        if self.target_tokens <= 0 or self.hard_max_tokens <= 0:
            raise ValueError("Token budgets must be positive.")
        if self.min_chunk_tokens > self.target_tokens:
            raise ValueError("min_chunk_tokens cannot exceed target_tokens.")
        if self.overlap_tokens < 0 or self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be in [0, target_tokens).")


@dataclass(slots=True)
class _Unit:
    """An atomic block produced by structural parsing (paragraph or heading)."""

    text: str
    is_heading: bool = False
    chapter: str | None = None
    section: str | None = None
    heading_title: str | None = None


@dataclass(slots=True)
class ChunkRecord:
    """One serialized chunk plus metadata.

    Fields added in step B (level, board, topic, chapter, section, chunk_type,
    visibility, content_hash, token_count) will land in a follow-up commit;
    they are already declared here so the schema is stable from day one. The
    chunker only populates the fields it knows about; downstream enrichment
    fills the rest.
    """

    chunk_id: str
    document_id: str
    source_file: str
    source_path: str
    subject: str
    content_type: str
    chunk_index: int
    total_chunks: int
    text: str
    char_count: int
    token_count: int
    chapter: str | None = None
    section: str | None = None
    topic: str | None = None
    level: str | None = None
    board: str | None = None
    chunk_type: str = "misc"
    visibility: str = "student_ok"
    content_hash: str = ""
    tags: list[str] = field(default_factory=list)


# --- Public helpers retained from v1 ---------------------------------------


def infer_subject_from_path(processed_text_path: Path) -> str:
    return processed_text_path.parent.name.lower()


def build_document_id(relative_path: Path) -> str:
    normalized = str(relative_path.with_suffix("")).replace("\\", "/").lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


# --- Tokenizer cache --------------------------------------------------------

_ENCODERS: dict[str, "object"] = {}


def _get_encoder(encoding_name: str):
    if encoding_name not in _ENCODERS:
        import tiktoken  # imported lazily; heavy

        _ENCODERS[encoding_name] = tiktoken.get_encoding(encoding_name)
    return _ENCODERS[encoding_name]


def _token_count(text: str, encoding_name: str) -> int:
    encoder = _get_encoder(encoding_name)
    return len(encoder.encode(text))


# --- Structural parsing -----------------------------------------------------


_HEADING_MAX_CHARS = 180  # real headings are short; longer paragraphs are body


def _classify_paragraph(paragraph: str) -> _Unit:
    """Tag a paragraph as either a heading or body text.

    A paragraph is only treated as a heading if (a) its first line matches a
    chapter/section pattern AND (b) the entire paragraph is short. This guards
    against exercise numbers like ``4.71 What volume of …?`` glued to a long
    run of subsequent questions being misclassified as a heading.
    """

    first_line = paragraph.splitlines()[0] if paragraph else ""
    if _TOC_DOTS.search(first_line):
        return _Unit(text=paragraph)

    is_short = len(paragraph) <= _HEADING_MAX_CHARS

    chapter_match = _CHAPTER_RE.match(first_line)
    if chapter_match and is_short:
        return _Unit(
            text=paragraph,
            is_heading=True,
            chapter=chapter_match.group(1),
            heading_title=(chapter_match.group(2) or "").strip(),
        )

    section_match = _SECTION_RE.match(first_line)
    if section_match and is_short:
        return _Unit(
            text=paragraph,
            is_heading=True,
            chapter=section_match.group(1),
            section=f"{section_match.group(1)}.{section_match.group(2)}",
            heading_title=(section_match.group(3) or "").strip(),
        )

    return _Unit(text=paragraph)


def _build_units(text: str) -> list[_Unit]:
    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [_classify_paragraph(p) for p in raw_paragraphs]


# --- Chunking core ----------------------------------------------------------


def _split_oversize_paragraph(
    paragraph: str,
    *,
    config: ChunkingConfig,
) -> list[str]:
    """Break a paragraph that is longer than hard_max_tokens by sentence.

    If a single sentence is still too long (e.g., a long bulleted line that
    PyMuPDF joined), fall back to a hard character split at a word boundary.
    The result is a list of pieces, each within hard_max_tokens.
    """

    sentences = [s.strip() for s in _SENTENCE_BOUNDARY.split(paragraph) if s.strip()]
    pieces: list[str] = []

    for sentence in sentences:
        if _token_count(sentence, config.encoding_name) <= config.hard_max_tokens:
            pieces.append(sentence)
            continue

        encoder = _get_encoder(config.encoding_name)
        tokens = encoder.encode(sentence)
        for start in range(0, len(tokens), config.hard_max_tokens):
            window = tokens[start : start + config.hard_max_tokens]
            pieces.append(encoder.decode(window).strip())

    return [p for p in pieces if p]


def _pack_units(units: list[_Unit], config: ChunkingConfig) -> list[dict]:
    """Pack atomic units into chunks within the token budget.

    Returns dicts: {text, token_count, char_count, chapter, section, topic}.
    Each new chunk records the chapter, section, and topic (heading title)
    that were active when it began.
    """

    chunks: list[dict] = []
    current_pieces: list[str] = []
    current_tokens = 0
    current_chapter: str | None = None
    current_section: str | None = None
    current_topic: str | None = None

    def _flush() -> None:
        nonlocal current_pieces, current_tokens
        if not current_pieces:
            return
        text = "\n\n".join(current_pieces).strip()
        if not text:
            current_pieces = []
            current_tokens = 0
            return
        chunks.append(
            {
                "text": text,
                "token_count": current_tokens,
                "char_count": len(text),
                "chapter": current_chapter,
                "section": current_section,
                "topic": current_topic,
            }
        )
        current_pieces = []
        current_tokens = 0

    for unit in units:
        if unit.is_heading:
            # Update current chapter/section context as soon as the heading
            # appears, so any chunk that's still accumulating gets attributed
            # to the new section once it flushes.
            if unit.chapter and unit.chapter != current_chapter:
                # New chapter — reset section/topic context so we don't carry
                # a stale section title from the previous chapter.
                current_chapter = unit.chapter
                current_section = None
                current_topic = None
            if unit.section:
                current_section = unit.section
            if unit.heading_title:
                current_topic = unit.heading_title

            heading_tokens = _token_count(unit.text, config.encoding_name)
            if heading_tokens > config.hard_max_tokens:
                # Defensive: a "heading" paragraph that's actually oversize
                # (mis-classification) should be treated as body and split.
                _flush()
                for sub in _split_oversize_paragraph(unit.text, config=config):
                    sub_tokens = _token_count(sub, config.encoding_name)
                    chunks.append(
                        {
                            "text": sub,
                            "token_count": sub_tokens,
                            "char_count": len(sub),
                            "chapter": current_chapter,
                            "section": current_section,
                            "topic": current_topic,
                        }
                    )
                continue
            # Soft heading boundary: only flush if the current chunk is already
            # big enough to stand on its own. Otherwise absorb the heading and
            # keep going — prevents 5-token chunks of just a section title.
            if current_tokens >= config.min_chunk_tokens:
                _flush()
            current_pieces.append(unit.text)
            current_tokens += heading_tokens
            continue

        piece_tokens = _token_count(unit.text, config.encoding_name)

        if piece_tokens > config.hard_max_tokens:
            # Oversize paragraph — split it and emit each sub-piece as its own
            # chunk. Flush whatever is currently accumulating first.
            _flush()
            for sub in _split_oversize_paragraph(unit.text, config=config):
                sub_tokens = _token_count(sub, config.encoding_name)
                chunks.append(
                    {
                        "text": sub,
                        "token_count": sub_tokens,
                        "char_count": len(sub),
                        "chapter": current_chapter,
                        "section": current_section,
                        "topic": current_topic,
                    }
                )
            continue

        projected = current_tokens + piece_tokens
        # Pre-flush either (a) we're already past min and target, or
        # (b) appending would blow past hard_max regardless of size — this
        # second branch prevents a tiny in-progress chunk from absorbing a
        # near-hard_max paragraph and ending up oversized.
        if (current_tokens >= config.min_chunk_tokens and projected > config.target_tokens) or (
            projected > config.hard_max_tokens
        ):
            _flush()

        current_pieces.append(unit.text)
        current_tokens += piece_tokens

        if current_tokens >= config.target_tokens:
            _flush()

    _flush()
    return _merge_tiny_neighbours(chunks, config)


def _merge_tiny_neighbours(chunks: list[dict], config: ChunkingConfig) -> list[dict]:
    """Merge each below-min chunk into its neighbour when they share a chapter.

    Single pass, left-to-right: if chunk[i] is below min_chunk_tokens and
    chunk[i] + chunk[i+1] fits within hard_max_tokens AND they share a chapter,
    merge them. Carries chapter/section from the larger (target) chunk.
    """
    if not chunks:
        return chunks

    out: list[dict] = []
    for chunk in chunks:
        if not out:
            out.append(dict(chunk))
            continue
        prev = out[-1]
        prev_under = prev["token_count"] < config.min_chunk_tokens
        same_chapter = (prev.get("chapter") == chunk.get("chapter"))
        combined_tokens = prev["token_count"] + chunk["token_count"]
        # Cap at target (not hard_max) so overlap has headroom to fit.
        if prev_under and same_chapter and combined_tokens <= config.target_tokens:
            merged_text = f"{prev['text']}\n\n{chunk['text']}"
            out[-1] = {
                "text": merged_text,
                "token_count": combined_tokens,
                "char_count": len(merged_text),
                # Adopt the section of whichever piece was the substantive one.
                "chapter": chunk.get("chapter") or prev.get("chapter"),
                "section": chunk.get("section") or prev.get("section"),
                "topic":   chunk.get("topic")   or prev.get("topic"),
            }
        else:
            out.append(dict(chunk))
    return out


def _apply_overlap(
    chunks: list[dict],
    overlap_tokens: int,
    encoding_name: str,
    hard_max_tokens: int,
) -> list[dict]:
    """Prepend a tail of the previous chunk's text to each chunk for continuity.

    The tail size is shrunk dynamically so the combined chunk never exceeds
    ``hard_max_tokens``. This means very-large chunks (right at hard_max from
    an oversize-paragraph split) get little or no overlap, while normal chunks
    get the full configured overlap.
    """

    if overlap_tokens <= 0 or len(chunks) < 2:
        return chunks

    encoder = _get_encoder(encoding_name)
    enriched: list[dict] = [dict(chunks[0])]
    for prev, curr in zip(chunks, chunks[1:]):
        budget = hard_max_tokens - curr["token_count"]
        if budget <= 0:
            enriched.append(dict(curr))
            continue
        tail_size = min(overlap_tokens, budget)
        tail_tokens = encoder.encode(prev["text"])[-tail_size:]
        tail_text = encoder.decode(tail_tokens).strip()
        if not tail_text:
            enriched.append(dict(curr))
            continue
        new_text = f"{tail_text}\n\n{curr['text']}"
        enriched.append(
            {
                **curr,
                "text": new_text,
                "token_count": len(encoder.encode(new_text)),
                "char_count": len(new_text),
            }
        )
    return enriched


def chunk_text(
    text: str,
    *,
    config: ChunkingConfig | None = None,
) -> list[dict]:
    """Chunk cleaned text. Returns a list of dicts with text + token/char/chapter/section."""

    config = config or ChunkingConfig()
    units = _build_units(text)
    if not units:
        return []
    chunks = _pack_units(units, config)
    return _apply_overlap(
        chunks,
        config.overlap_tokens,
        config.encoding_name,
        config.hard_max_tokens,
    )


# --- ChunkRecord assembly + JSONL I/O --------------------------------------


def _content_hash(text: str) -> str:
    """Normalize whitespace + casing, then SHA1. Used for cross-doc dedup."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def build_chunk_records(
    processed_text_path: Path,
    text: str,
    *,
    source_root: Path,
    config: ChunkingConfig | None = None,
    content_type: str = "textbook",
    board: str | None = "WAEC",
    drop_low_quality: bool = True,
) -> list[ChunkRecord]:
    """Build structured ChunkRecord objects for one processed text document.

    Each chunk is run through the heuristic classifier to populate
    ``chunk_type``, ``visibility``, and ``tags``, and the quality score is
    used to drop low-signal chunks (TOC fragments, boilerplate-only blocks)
    when ``drop_low_quality=True``.

    Indices are assigned AFTER quality filtering so ``chunk_index`` /
    ``total_chunks`` reflect what actually gets emitted.
    """

    config = config or ChunkingConfig()
    relative_path = processed_text_path.resolve().relative_to(source_root.resolve())
    subject = infer_subject_from_path(processed_text_path)
    document_id = build_document_id(relative_path)
    raw_chunks = chunk_text(text, config=config)

    classified: list[tuple[dict, object]] = []
    dropped_count = 0
    for chunk in raw_chunks:
        classification = classify_chunk(chunk["text"])
        if drop_low_quality and classification.quality_score < QUALITY_DROP_THRESHOLD:
            dropped_count += 1
            continue
        classified.append((chunk, classification))

    total_chunks = len(classified)
    # Visibility is determined by source content_type — NOT by chunk content.
    # Every chunk from a textbook is student_ok; every chunk from a teacher
    # guide is teacher_only. Source-level decision; classifier never overrides.
    visibility = visibility_for_content_type(content_type)
    records: list[ChunkRecord] = []
    for index, (chunk, classification) in enumerate(classified):
        content_hash = _content_hash(chunk["text"])
        chunk_id = f"{document_id}-{index:04d}-{content_hash[:12]}"
        records.append(
            ChunkRecord(
                chunk_id=chunk_id,
                document_id=document_id,
                source_file=processed_text_path.name,
                source_path=str(relative_path),
                subject=subject,
                content_type=content_type,
                chunk_index=index,
                total_chunks=total_chunks,
                text=chunk["text"],
                char_count=int(chunk["char_count"]),
                token_count=int(chunk["token_count"]),
                chapter=chunk.get("chapter"),
                section=chunk.get("section"),
                topic=chunk.get("topic"),
                board=board,
                chunk_type=classification.chunk_type,
                visibility=visibility,
                content_hash=content_hash,
                tags=list(classification.tags),
            )
        )

    LOGGER.info(
        "Prepared %s chunk(s) for %s (dropped %s low-quality)",
        total_chunks,
        processed_text_path,
        dropped_count,
    )
    return records


def write_chunk_records(
    chunk_records: Iterable[ChunkRecord],
    destination_path: Path,
    *,
    overwrite: bool = True,
) -> None:
    """Write chunk records to a JSONL file."""

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists() and not overwrite:
        raise FileExistsError(f"Chunk output already exists: {destination_path}")

    with destination_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in chunk_records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
