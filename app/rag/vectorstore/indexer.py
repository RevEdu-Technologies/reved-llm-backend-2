"""Chunk embedding and Pinecone upsert orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.rag.embedding import Embedder
from app.rag.vectorstore.store import PineconeVectorStore


class ChunkIndexingError(RuntimeError):
    """Raised when chunk loading or indexing fails."""


@dataclass(slots=True)
class ChunkIndexingResult:
    """Summary of indexing work for a single chunk JSONL file."""

    chunk_file_path: str
    source_file: str
    subject: str
    status: str
    indexed_count: int
    namespace: str
    notes: str = ""


class ChunkVectorIndexer:
    """Read chunk JSONL files, embed their text, and upsert vectors."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: PineconeVectorStore,
        namespace: str,
        embedding_batch_size: int,
        upsert_batch_size: int,
        include_chunk_text: bool = False,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.namespace = namespace
        self.embedding_batch_size = embedding_batch_size
        self.upsert_batch_size = upsert_batch_size
        self.include_chunk_text = include_chunk_text

    def index_chunk_directory(self, chunk_root: Path) -> list[ChunkIndexingResult]:
        """Index every chunk JSONL file under the given directory."""

        results: list[ChunkIndexingResult] = []
        for chunk_file in sorted(chunk_root.rglob("*.jsonl")):
            results.append(self.index_chunk_file(chunk_file))
        return results

    def index_chunk_file(self, chunk_file: Path) -> ChunkIndexingResult:
        """Index a single chunk JSONL file."""

        chunk_records = list(self._load_chunk_records(chunk_file))
        if not chunk_records:
            return ChunkIndexingResult(
                chunk_file_path=str(chunk_file),
                source_file=f"{chunk_file.stem}.txt",
                subject=chunk_file.parent.name.lower(),
                status="failed_indexing",
                indexed_count=0,
                namespace=self.namespace,
                notes="Chunk file contained no records.",
            )

        indexed_count = 0
        for batch in _batched(chunk_records, self.embedding_batch_size):
            texts = [record["text"] for record in batch]
            embeddings = self.embedder.embed_texts(texts).vectors

            vectors = [
                {
                    "id": record["chunk_id"],
                    "values": vector,
                    "metadata": self._build_metadata(record),
                }
                for record, vector in zip(batch, embeddings, strict=True)
            ]

            for upsert_batch in _batched(vectors, self.upsert_batch_size):
                indexed_count += self.vector_store.upsert(upsert_batch, namespace=self.namespace)

        first_record = chunk_records[0]
        return ChunkIndexingResult(
            chunk_file_path=str(chunk_file),
            source_file=str(first_record["source_file"]),
            subject=str(first_record["subject"]),
            status="indexed",
            indexed_count=indexed_count,
            namespace=self.namespace,
            notes=f"Indexed {indexed_count} chunk vector(s).",
        )

    def _load_chunk_records(self, chunk_file: Path) -> Iterator[dict[str, Any]]:
        try:
            with chunk_file.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    record = json.loads(stripped)
                    self._validate_chunk_record(record, chunk_file, line_number)
                    yield record
        except json.JSONDecodeError as exc:
            raise ChunkIndexingError(f"Invalid JSONL in {chunk_file}: {exc}") from exc

    @staticmethod
    def _validate_chunk_record(record: dict[str, Any], chunk_file: Path, line_number: int) -> None:
        required_fields = {
            "chunk_id",
            "document_id",
            "source_file",
            "source_path",
            "subject",
            "content_type",
            "chunk_index",
            "total_chunks",
            "text",
            "char_count",
        }
        missing = required_fields.difference(record)
        if missing:
            raise ChunkIndexingError(
                f"Chunk record in {chunk_file} line {line_number} is missing fields: {sorted(missing)}"
            )

    def _build_metadata(self, record: dict[str, Any]) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "source_file": record["source_file"],
            "source_path": record["source_path"],
            "subject": record["subject"],
            "content_type": record["content_type"],
            "chunk_index": int(record["chunk_index"]),
            "total_chunks": int(record["total_chunks"]),
            "document_id": record["document_id"],
            "char_count": int(record["char_count"]),
        }
        # Optional enrichment fields — only emit them if present so Pinecone
        # doesn't get null values (its filter operators handle missing keys
        # cleanly via $exists).
        for key, transform in (
            ("token_count", int),
            ("chapter", str),
            ("section", str),
            ("topic", str),
            ("level", str),
            ("board", str),
            ("chunk_type", str),
            ("visibility", str),
            ("content_hash", str),
        ):
            value = record.get(key)
            if value not in (None, "", []):
                metadata[key] = transform(value)
        tags = record.get("tags")
        if tags:
            metadata["tags"] = list(tags)
        if self.include_chunk_text:
            metadata["text"] = record["text"]
        return metadata


def _batched(items: Sequence[Any], batch_size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])
