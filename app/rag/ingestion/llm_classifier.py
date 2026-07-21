"""LLM-backed fallback classifier for chunks the heuristic tags as ``misc``.

The heuristic in ``app.rag.ingestion.classifier`` handles the unambiguous
cases (definitions, examples, exercises, etc.). Whatever it can't decide is
left as ``misc`` — and that's almost always explanatory prose that we'd like
to label more precisely so retrieval filters and the prompt layer can use it.

This module batches misc chunks and sends them to Groq for classification.
Failures degrade gracefully: a chunk that the LLM can't classify stays
``misc``, so the rest of the pipeline keeps working even if Groq is down.

Usage:

    from app.rag.ingestion.llm_classifier import reclassify_misc_chunks
    reclassify_misc_chunks(records, client=GroqChatClient.from_settings(settings))

In-place: ``records[i].chunk_type`` is updated where applicable.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterable, Sequence

from app.llm.client import GroqChatClient, LLMClientError
from app.rag.ingestion.chunker import ChunkRecord

LOGGER = logging.getLogger(__name__)

# Labels the LLM may emit. Must stay aligned with filters.VALID_CHUNK_TYPES.
LLM_CHUNK_TYPES = (
    "definition",
    "explanation",
    "formula",
    "worked_example",
    "exercise",
    "solution",
    "figure",
    "summary",
    "misc",
)

_SYSTEM_PROMPT = (
    "You are classifying short chunks of educational text from secondary-school "
    "textbooks. For each chunk, output exactly one label from this list:\n"
    "- definition: defines a term, concept, or unit (often phrased as 'X is defined as').\n"
    "- explanation: explains a concept, process, phenomenon, or principle in prose. "
    "Use this label for the dominant body-text content.\n"
    "- worked_example: a problem followed by a step-by-step solution.\n"
    "- exercise: a question or problem posed to the reader to solve.\n"
    "- solution: a stand-alone answer or solution to a problem.\n"
    "- formula: dominated by an equation and its variable definitions.\n"
    "- figure: a figure caption, diagram description, or table.\n"
    "- summary: a chapter summary, review, or key-points list.\n"
    "- misc: doesn't clearly fit any of the above.\n\n"
    "Return JSON: an object whose keys are the chunk IDs you were given and whose "
    "values are the labels. Output the JSON object only — no commentary."
)

# Trim chunk previews so prompts stay compact. 300 chars is enough signal for
# a 9-way label classifier and keeps us under Groq's per-minute token budget.
_PREVIEW_CHARS = 300

# Llama 3.1 8B Instant is the right tool for this job: single-word label
# classification, much higher TPM ceiling than the 70B, and quality is more
# than sufficient for picking from a 9-element list. Override via the
# ``model`` kwarg if needed.
_DEFAULT_CLASSIFIER_MODEL = "llama-3.1-8b-instant"


def reclassify_misc_chunks(
    records: list[ChunkRecord],
    *,
    client: GroqChatClient | None = None,
    batch_size: int = 20,
    request_interval_seconds: float = 2.1,
    model: str | None = _DEFAULT_CLASSIFIER_MODEL,
    max_completion_tokens: int = 400,
    max_retries: int = 3,
    retry_backoff_seconds: float = 15.0,
) -> dict[str, int]:
    """Re-classify chunks currently tagged ``misc`` using a Groq LLM.

    Returns a counter ``{label: count_reclassified}`` summarising what changed.
    Chunks the LLM can't classify (bad JSON, missing key, unknown label, API
    failure after retries) stay as ``misc``. The function is safe to re-run.

    Defaults are tuned for Groq's free tier:
      * ``model=llama-3.1-8b-instant`` — 30,000 TPM ceiling (5× the 70B).
      * ``batch_size=20`` × ``_PREVIEW_CHARS=300`` ≈ 1.5k tokens/call.
      * ``request_interval_seconds=2.1`` ≈ 28 calls/min (under 30 RPM).
      * On a batch failure, retry up to ``max_retries`` times with a long
        backoff (default 15 s) — typically clears transient TPM/RPM windows.
    """

    if not records:
        return {}

    if client is None:
        try:
            from app.core.config import get_settings

            client = GroqChatClient.from_settings(get_settings())
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("LLM client unavailable; leaving misc chunks unchanged: %s", exc)
            return {}

    misc_indices = [i for i, r in enumerate(records) if r.chunk_type == "misc"]
    if not misc_indices:
        return {}

    LOGGER.info(
        "LLM-classifying %s misc chunk(s) in batches of %s (model=%s)",
        len(misc_indices),
        batch_size,
        model,
    )

    summary: dict[str, int] = {}
    failed_batches = 0
    for batch_start in range(0, len(misc_indices), batch_size):
        batch_indices = misc_indices[batch_start : batch_start + batch_size]
        labels = _classify_batch_with_retry(
            [records[i] for i in batch_indices],
            client=client,
            model=model,
            max_completion_tokens=max_completion_tokens,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        if all(label == "misc" for label in labels):
            failed_batches += 1
        for record_idx, label in zip(batch_indices, labels):
            if not label or label == "misc":
                continue
            records[record_idx].chunk_type = label
            summary[label] = summary.get(label, 0) + 1
        if batch_start + batch_size < len(misc_indices):
            time.sleep(request_interval_seconds)

    LOGGER.info(
        "LLM reclassification summary: %s (failed_batches=%s)",
        summary,
        failed_batches,
    )
    return summary


# --- Internals --------------------------------------------------------------


def _classify_batch_with_retry(
    batch: Sequence[ChunkRecord],
    *,
    client: GroqChatClient,
    model: str | None,
    max_completion_tokens: int,
    max_retries: int,
    retry_backoff_seconds: float,
) -> list[str]:
    """Classify one batch, retrying on transient failures.

    A batch is retried until ``max_retries`` is reached (or the call succeeds).
    Between attempts we wait ``retry_backoff_seconds`` so the TPM/RPM window
    has a chance to roll over. After the final failure, every record in the
    batch is left as 'misc'.
    """

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return _classify_batch(
                batch,
                client=client,
                model=model,
                max_completion_tokens=max_completion_tokens,
            )
        except LLMClientError as exc:
            last_error = exc
            if attempt < max_retries:
                LOGGER.warning(
                    "Groq batch failed (attempt %s/%s): %s — backing off %.0fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    retry_backoff_seconds,
                )
                time.sleep(retry_backoff_seconds)
                continue
            break

    LOGGER.warning(
        "Groq batch failed after %s attempts (%s) — leaving %s chunks as misc",
        max_retries + 1,
        last_error,
        len(batch),
    )
    return ["misc"] * len(batch)


def _classify_batch(
    batch: Sequence[ChunkRecord],
    *,
    client: GroqChatClient,
    model: str | None,
    max_completion_tokens: int,
) -> list[str]:
    """Classify one batch. Raises ``LLMClientError`` on Groq API failure."""

    payload = {
        str(idx): _preview(record.text)
        for idx, record in enumerate(batch)
    }
    user_prompt = f"Classify these chunks:\n{json.dumps(payload, ensure_ascii=False)}"

    response = client.generate(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=model,
        temperature=0.0,
        max_completion_tokens=max_completion_tokens,
        response_format={"type": "json_object"},
    )

    parsed = _parse_label_map(response.text, expected_keys=set(payload.keys()))
    return [parsed.get(str(idx), "misc") for idx in range(len(batch))]


def _preview(text: str) -> str:
    """Trim a chunk to ``_PREVIEW_CHARS``, preserving the head."""
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS].rstrip() + " …"


def _parse_label_map(raw: str, *, expected_keys: set[str]) -> dict[str, str]:
    """Parse the LLM's JSON output into ``{id: label}``.

    Tolerates malformed responses: if the response isn't valid JSON or doesn't
    return an object, we recover the JSON object substring or fall back to an
    empty dict. Unknown labels are coerced to ``misc``.
    """

    text = (raw or "").strip()
    if not text:
        return {}

    # Strip code fences if Groq wrapped the JSON.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    valid_labels = set(LLM_CHUNK_TYPES)
    for key, value in data.items():
        if not isinstance(value, str):
            continue
        label = value.strip().lower().replace(" ", "_")
        if label not in valid_labels:
            label = "misc"
        result[str(key)] = label

    return result


def _iter_misc_records(records: Iterable[ChunkRecord]) -> Iterable[ChunkRecord]:
    """Iterator over records whose chunk_type is currently 'misc'."""
    return (r for r in records if r.chunk_type == "misc")
