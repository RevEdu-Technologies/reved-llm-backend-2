"""Bulk re-embed every existing chunk JSONL with BAAI/bge-base-en-v1.5
and upsert into the new 768-dim Pinecone index (default: reved-index-v2).

Reads from:  data/chunks/textbooks/**/*.jsonl
Writes to:   the Pinecone index named by PINECONE_INDEX_NAME (default reved-index-v2)
Tracks:      data/migration_v2_status.csv  (one row per chunk JSONL file)

Resumable: a chunk file whose row in migration_v2_status.csv is marked
``indexed`` is skipped on subsequent runs. Pinecone upserts are idempotent
keyed by chunk_id, so partial runs are safe.

Run from the repo root:
    python scripts/reembed_with_bge.py

Required env (in .env):
    PINECONE_API_KEY      Pinecone account key
    DATABASE_URL          Required by Settings; not actually used here
    GROQ_API_KEY          Required by Settings; not actually used here

Optional env:
    EMBEDDING_BACKEND        default: local
    HF_EMBEDDING_MODEL       default: BAAI/bge-base-en-v1.5
    PINECONE_INDEX_NAME      default: reved-index-v2
    PINECONE_DIMENSION       default: 768
    PINECONE_NAMESPACE       default: textbooks
    EMBEDDING_DEVICE         default: cpu  (set to "cuda" if a GPU is available)
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError, get_settings  # noqa: E402
from app.rag.embedding import get_embedder  # noqa: E402
from app.rag.vectorstore.indexer import ChunkVectorIndexer  # noqa: E402
from app.rag.vectorstore.store import PineconeVectorStore  # noqa: E402

LOGGER = logging.getLogger("reembed_with_bge")

STATUS_COLUMNS = [
    "chunk_file",
    "subject",
    "source_file",
    "indexing_status",
    "indexed_vector_count",
    "pinecone_index",
    "pinecone_namespace",
    "embedding_model",
    "last_run_at",
    "notes",
]


@dataclass(slots=True)
class StatusRow:
    chunk_file: str
    subject: str = ""
    source_file: str = ""
    indexing_status: str = ""
    indexed_vector_count: str = ""
    pinecone_index: str = ""
    pinecone_namespace: str = ""
    embedding_model: str = ""
    last_run_at: str = ""
    notes: str = ""


class MigrationStatus:
    """CSV-backed status tracker for the v2 re-embed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, StatusRow] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for raw in reader:
                row = StatusRow(**{column: raw.get(column, "") for column in STATUS_COLUMNS})
                self.rows[row.chunk_file] = row

    def get(self, chunk_file: str) -> StatusRow | None:
        return self.rows.get(chunk_file)

    def upsert(self, row: StatusRow) -> None:
        self.rows[row.chunk_file] = row

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=STATUS_COLUMNS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: r.chunk_file.lower()):
                writer.writerow({column: getattr(row, column) for column in STATUS_COLUMNS})


def _count_chunks(chunk_file: Path) -> int:
    total = 0
    with chunk_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                total += 1
    return total


def _source_file_from_jsonl(chunk_file: Path) -> str:
    """Peek the first chunk record to recover the source filename."""
    with chunk_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                try:
                    record = json.loads(stripped)
                    return str(record.get("source_file", f"{chunk_file.stem}.txt"))
                except json.JSONDecodeError:
                    break
    return f"{chunk_file.stem}.txt"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    chunk_root = PROJECT_ROOT / "data" / "chunks" / "textbook"
    if not chunk_root.exists():
        raise SystemExit(f"No chunk directory found at {chunk_root}.")

    chunk_files = sorted(chunk_root.rglob("*.jsonl"))
    if not chunk_files:
        raise SystemExit(f"No JSONL chunk files found under {chunk_root}.")

    status = MigrationStatus(PROJECT_ROOT / "data" / "migration_v2_status.csv")

    LOGGER.info(
        "Re-embed plan: %s chunk file(s) | model=%s | index=%s | dim=%s | namespace=%s",
        len(chunk_files),
        settings.hf_embedding_model,
        settings.pinecone_index_name,
        settings.pinecone_dimension,
        settings.pinecone_namespace,
    )

    embedder = get_embedder(settings)
    vector_store = PineconeVectorStore.from_settings(settings)
    vector_store.ensure_index()
    indexer = ChunkVectorIndexer(
        embedder=embedder,
        vector_store=vector_store,
        namespace=settings.pinecone_namespace,
        embedding_batch_size=settings.hf_embedding_batch_size,
        upsert_batch_size=settings.pinecone_upsert_batch_size,
        include_chunk_text=settings.pinecone_include_chunk_text,
    )

    total_indexed = 0
    for chunk_file in chunk_files:
        relative = chunk_file.relative_to(PROJECT_ROOT).as_posix()
        existing = status.get(relative)
        if (
            existing
            and existing.indexing_status == "indexed"
            and existing.pinecone_index == settings.pinecone_index_name
            and existing.embedding_model == settings.hf_embedding_model
        ):
            LOGGER.info("Skipping already migrated: %s", relative)
            total_indexed += int(existing.indexed_vector_count or 0)
            continue

        expected_chunk_count = _count_chunks(chunk_file)
        source_file = _source_file_from_jsonl(chunk_file)
        subject = chunk_file.parent.name.lower()

        LOGGER.info(
            "Indexing %s (subject=%s, chunks=%s)",
            relative,
            subject,
            expected_chunk_count,
        )
        try:
            result = indexer.index_chunk_file(chunk_file)
        except Exception as exc:  # noqa: BLE001 - want a clear failure row
            LOGGER.exception("Failed to index %s", relative)
            status.upsert(
                StatusRow(
                    chunk_file=relative,
                    subject=subject,
                    source_file=source_file,
                    indexing_status="failed",
                    indexed_vector_count="0",
                    pinecone_index=settings.pinecone_index_name,
                    pinecone_namespace=settings.pinecone_namespace,
                    embedding_model=settings.hf_embedding_model,
                    last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    notes=str(exc),
                )
            )
            status.save()
            continue

        status.upsert(
            StatusRow(
                chunk_file=relative,
                subject=subject,
                source_file=source_file,
                indexing_status=result.status,
                indexed_vector_count=str(result.indexed_count),
                pinecone_index=settings.pinecone_index_name,
                pinecone_namespace=settings.pinecone_namespace,
                embedding_model=settings.hf_embedding_model,
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                notes=result.notes,
            )
        )
        status.save()
        total_indexed += result.indexed_count

    LOGGER.info(
        "Done. %s vector(s) present in index '%s' across %s file(s).",
        total_indexed,
        settings.pinecone_index_name,
        len(chunk_files),
    )


if __name__ == "__main__":
    main()
