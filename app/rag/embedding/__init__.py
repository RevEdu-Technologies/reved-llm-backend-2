"""Embedding providers for the RAG pipeline."""

from __future__ import annotations

from typing import Protocol, Sequence

from app.core.config import Settings

from .embedder import EmbeddingError, EmbeddingResult, HuggingFaceInferenceEmbedder
from .local_embedder import LocalSentenceTransformerEmbedder


class Embedder(Protocol):
    """Common interface every embedding backend must satisfy."""

    expected_dimension: int

    def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult: ...
    def embed_query(self, text: str) -> list[float]: ...


def get_embedder(settings: Settings) -> Embedder:
    """Return the configured embedding backend.

    The choice is controlled by ``EMBEDDING_BACKEND`` in the environment:
    ``local`` (default) loads the model via sentence-transformers; ``hf_api``
    keeps the legacy HTTP-based HuggingFace Inference Router path.
    """

    if settings.embedding_backend == "local":
        return LocalSentenceTransformerEmbedder.from_settings(settings)
    return HuggingFaceInferenceEmbedder.from_settings(settings)


__all__ = [
    "Embedder",
    "EmbeddingError",
    "EmbeddingResult",
    "HuggingFaceInferenceEmbedder",
    "LocalSentenceTransformerEmbedder",
    "get_embedder",
]
