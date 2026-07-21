"""Embedding provider abstraction backed by Hugging Face Inference API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import quote

from app.core.config import Settings

LOGGER = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when the embedding provider fails or returns invalid output."""


@dataclass(slots=True)
class EmbeddingResult:
    """Embedding vectors returned for a batch of input texts."""

    vectors: list[list[float]]


class HuggingFaceInferenceEmbedder:
    """Generate embeddings with Hugging Face Inference API feature extraction."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        expected_dimension: int = 768,
        normalize: bool = True,
        query_prefix: str = "",
        passage_prefix: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.expected_dimension = expected_dimension
        self.normalize = normalize
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self._endpoint = (
            "https://router.huggingface.co/hf-inference/models/"
            f"{quote(model, safe='')}/pipeline/feature-extraction"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "HuggingFaceInferenceEmbedder":
        """Construct the embedder from application settings."""

        return cls(
            api_key=settings.huggingface_api_key,
            model=settings.hf_embedding_model,
            expected_dimension=settings.pinecone_dimension,
            normalize=settings.hf_embedding_normalize,
            query_prefix=settings.embedding_query_prefix,
            passage_prefix=settings.embedding_passage_prefix,
        )

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string, applying the model's query prefix."""

        prefixed = f"{self.query_prefix}{text}" if self.query_prefix else text
        result = self.embed_texts([prefixed])
        if not result.vectors:
            raise EmbeddingError("Embedding provider returned no vector for query.")
        return result.vectors[0]

    def embed_texts(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed a batch of passage texts using the configured Hugging Face model."""

        batch = [text for text in texts if text.strip()]
        if not batch:
            return EmbeddingResult(vectors=[])

        if self.passage_prefix:
            batch = [f"{self.passage_prefix}{text}" for text in batch]

        payload = {
            "inputs": batch,
            "normalize": self.normalize,
        }

        try:
            import requests

            session = requests.Session()
            # Avoid inheriting broken shell proxy settings for Hugging Face calls.
            session.trust_env = False
            response = session.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            payload_data: object = response.content
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise EmbeddingError(f"Embedding request failed for model '{self.model}'.") from exc

        vectors = self._coerce_vectors(payload_data, expected_count=len(batch))
        LOGGER.info("Generated %s embedding vector(s) with %s", len(vectors), self.model)
        return EmbeddingResult(vectors=vectors)

    def _coerce_vectors(self, response: object, *, expected_count: int) -> list[list[float]]:
        if isinstance(response, (bytes, bytearray)):
            response = json.loads(response.decode("utf-8"))
        if hasattr(response, "tolist"):
            response = response.tolist()

        if not isinstance(response, list):
            raise EmbeddingError("Unexpected embedding response format; expected a list.")

        if expected_count == 1 and response and all(isinstance(value, (float, int)) for value in response):
            vectors = [[float(value) for value in response]]
        else:
            if not response or not all(isinstance(item, list) for item in response):
                raise EmbeddingError("Unexpected embedding response shape for batch feature extraction.")
            vectors = []
            for item in response:
                if not all(isinstance(value, (float, int)) for value in item):
                    raise EmbeddingError("Embedding response contained non-numeric values.")
                vectors.append([float(value) for value in item])

        if len(vectors) != expected_count:
            raise EmbeddingError(
                f"Expected {expected_count} embeddings but received {len(vectors)}."
            )

        for vector in vectors:
            if len(vector) != self.expected_dimension:
                raise EmbeddingError(
                    f"Expected embedding dimension {self.expected_dimension} but received {len(vector)}."
                )

        return vectors
