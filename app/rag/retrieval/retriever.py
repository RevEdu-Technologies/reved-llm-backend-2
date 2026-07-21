"""Reusable semantic retrieval for the MVP RAG pipeline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.core.config import Settings
from app.core.tracing import start_span
from app.rag.embedding import Embedder, get_embedder
from app.rag.retrieval.filters import build_metadata_filter
from app.rag.vectorstore.store import PineconeVectorStore


@dataclass(slots=True)
class RetrievalResult:
    """One semantic retrieval match returned to the caller."""

    score: float
    chunk_id: str
    document_id: str
    source_file: str
    subject: str
    content_type: str
    chunk_index: int
    text: str
    chapter: str | None = None
    section: str | None = None
    topic: str | None = None
    chunk_type: str | None = None
    visibility: str | None = None
    token_count: int | None = None


class PineconeRetriever:
    """Embed queries and retrieve matching textbook chunks from Pinecone."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: PineconeVectorStore,
        chunk_root: Path,
        namespace: str,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.chunk_root = chunk_root
        self.namespace = namespace
        self._chunk_cache: dict[Path, dict[str, str]] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "PineconeRetriever":
        """Construct a retriever from application settings."""

        root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
        return cls(
            embedder=get_embedder(settings),
            vector_store=PineconeVectorStore.from_settings(settings),
            chunk_root=root / "data" / "chunks" / "textbook",
            namespace=settings.pinecone_namespace,
        )

    def retrieve(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        subject: str | None = None,
        namespace: str | None = None,
        role: str | None = None,
        chunk_type: str | Sequence[str] | None = None,
        chapter: str | None = None,
        section: str | None = None,
        topic: str | None = None,
        level: str | None = None,
    ) -> list[RetrievalResult]:
        """Return top semantic matches for a user query.

        ``role`` is a high-level shortcut: ``role='student'`` enforces
        ``visibility=student_ok`` at the Pinecone metadata filter so
        teacher-only chunks are never returned to a student. Pass
        ``role='teacher'`` (or omit) for unrestricted retrieval.
        """

        if not query_text.strip():
            raise ValueError("query_text must not be empty.")
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero.")

        # Manual spans: the Pinecone SDK uses its own urllib3 path, so
        # the httpx instrumentation doesn't catch it. The embedder runs
        # on CPU (HuggingFace) and can take 50–200ms — worth its own
        # span. Both attributes use the OTEL ``db.*`` / ``net.*`` naming
        # convention loosely; semantic conventions for vector DBs are
        # still in draft as of OTEL 1.39 so we keep names readable.
        with start_span(
            "rag.embed_query",
            **{"rag.query_length": len(query_text)},
        ):
            query_vector = self.embedder.embed_query(query_text)

        metadata_filter = build_metadata_filter(
            subject=subject,
            role=role,
            chunk_type=chunk_type,
            chapter=chapter,
            section=section,
            topic=topic,
            level=level,
        )
        pinecone_namespace = namespace or self.namespace

        with start_span(
            "pinecone.query",
            **{
                "pinecone.namespace": pinecone_namespace or "",
                "pinecone.top_k": top_k,
                "pinecone.subject": subject or "",
                "pinecone.role": role or "",
            },
        ):
            response = self.vector_store.query(
                vector=query_vector,
                top_k=top_k,
                namespace=pinecone_namespace,
                metadata_filter=metadata_filter,
                include_metadata=True,
                include_values=False,
            )

        matches = self._extract_matches(response)
        results = [self._build_result(match) for match in matches]
        return results

    def _build_result(self, match: Any) -> RetrievalResult:
        chunk_id = self._get_field(match, "id")
        score = float(self._get_field(match, "score", 0.0))
        metadata = self._get_metadata(match)

        subject = str(metadata.get("subject", ""))
        source_file = str(metadata.get("source_file", ""))
        text = str(metadata.get("text") or self._resolve_chunk_text(subject, source_file, chunk_id))

        token_count = metadata.get("token_count")
        return RetrievalResult(
            score=score,
            chunk_id=str(chunk_id),
            document_id=str(metadata.get("document_id", "")),
            source_file=source_file,
            subject=subject,
            content_type=str(metadata.get("content_type", "")),
            chunk_index=int(metadata.get("chunk_index", 0)),
            text=text,
            chapter=(str(metadata["chapter"]) if metadata.get("chapter") else None),
            section=(str(metadata["section"]) if metadata.get("section") else None),
            topic=(str(metadata["topic"]) if metadata.get("topic") else None),
            chunk_type=(str(metadata["chunk_type"]) if metadata.get("chunk_type") else None),
            visibility=(str(metadata["visibility"]) if metadata.get("visibility") else None),
            token_count=(int(token_count) if token_count is not None else None),
        )

    def _resolve_chunk_text(self, subject: str, source_file: str, chunk_id: str) -> str:
        chunk_file = self.chunk_root / subject / f"{Path(source_file).stem}.jsonl"
        if chunk_file not in self._chunk_cache:
            self._chunk_cache[chunk_file] = self._load_chunk_texts(chunk_file)
        return self._chunk_cache[chunk_file].get(chunk_id, "")

    @staticmethod
    def _load_chunk_texts(chunk_file: Path) -> dict[str, str]:
        if not chunk_file.exists():
            return {}

        chunk_map: dict[str, str] = {}
        with chunk_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                chunk_map[str(record.get("chunk_id", ""))] = str(record.get("text", ""))
        return chunk_map

    @staticmethod
    def _extract_matches(response: Any) -> list[Any]:
        matches = getattr(response, "matches", None)
        if matches is not None:
            return list(matches)
        if isinstance(response, dict):
            return list(response.get("matches", []))
        return []

    @staticmethod
    def _get_metadata(match: Any) -> dict[str, Any]:
        metadata = getattr(match, "metadata", None)
        if metadata is not None:
            return dict(metadata)
        if isinstance(match, dict):
            return dict(match.get("metadata", {}))
        return {}

    @staticmethod
    def _get_field(match: Any, field: str, default: Any = "") -> Any:
        value = getattr(match, field, None)
        if value is not None:
            return value
        if isinstance(match, dict):
            return match.get(field, default)
        return default
