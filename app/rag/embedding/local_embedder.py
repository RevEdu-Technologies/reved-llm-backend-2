"""Local embedding provider backed by sentence-transformers."""

from __future__ import annotations

import logging
from typing import Sequence

from app.core.config import Settings
from app.rag.embedding.embedder import EmbeddingError, EmbeddingResult

LOGGER = logging.getLogger(__name__)


class LocalSentenceTransformerEmbedder:
    """Generate embeddings locally via the sentence-transformers library.

    The model is loaded lazily on first use so importing this module does not
    pull torch into memory unless an embedding call is actually made.
    """

    def __init__(
        self,
        *,
        model: str,
        expected_dimension: int,
        normalize: bool = True,
        device: str = "cpu",
        batch_size: int = 32,
        query_prefix: str = "",
        passage_prefix: str = "",
    ) -> None:
        self.model_name = model
        self.expected_dimension = expected_dimension
        self.normalize = normalize
        self.device = device
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self._model = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "LocalSentenceTransformerEmbedder":
        return cls(
            model=settings.hf_embedding_model,
            expected_dimension=settings.pinecone_dimension,
            normalize=settings.hf_embedding_normalize,
            device=settings.embedding_device,
            batch_size=settings.hf_embedding_batch_size,
            query_prefix=settings.embedding_query_prefix,
            passage_prefix=settings.embedding_passage_prefix,
        )

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise EmbeddingError(
                "sentence-transformers is required for the local embedding backend. "
                "Install it with `pip install sentence-transformers`."
            ) from exc

        LOGGER.info("Loading sentence-transformers model '%s' on %s", self.model_name, self.device)
        self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed a batch of passage texts."""

        batch = [text for text in texts if text and text.strip()]
        if not batch:
            return EmbeddingResult(vectors=[])

        if self.passage_prefix:
            batch = [f"{self.passage_prefix}{text}" for text in batch]

        model = self._ensure_model()
        try:
            vectors = model.encode(
                batch,
                batch_size=self.batch_size,
                normalize_embeddings=self.normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except Exception as exc:  # pragma: no cover - runtime-dependent
            raise EmbeddingError(
                f"Local embedding failed for model '{self.model_name}'."
            ) from exc

        vector_list = [vector.tolist() for vector in vectors]
        self._validate_dimensions(vector_list, expected_count=len(batch))
        return EmbeddingResult(vectors=vector_list)

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string, applying the model's query prefix."""

        if not text or not text.strip():
            raise EmbeddingError("Cannot embed an empty query.")

        prefixed = f"{self.query_prefix}{text}" if self.query_prefix else text
        model = self._ensure_model()
        try:
            vector = model.encode(
                [prefixed],
                batch_size=1,
                normalize_embeddings=self.normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
        except Exception as exc:  # pragma: no cover - runtime-dependent
            raise EmbeddingError(
                f"Local embedding failed for model '{self.model_name}'."
            ) from exc

        vector_list = vector.tolist()
        self._validate_dimensions([vector_list], expected_count=1)
        return vector_list

    def _validate_dimensions(self, vectors: list[list[float]], *, expected_count: int) -> None:
        if len(vectors) != expected_count:
            raise EmbeddingError(
                f"Expected {expected_count} embeddings but received {len(vectors)}."
            )
        for vector in vectors:
            if len(vector) != self.expected_dimension:
                raise EmbeddingError(
                    f"Expected embedding dimension {self.expected_dimension} but received {len(vector)}."
                )
