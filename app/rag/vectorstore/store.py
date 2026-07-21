"""Pinecone-backed vector store helpers."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.config import Settings

LOGGER = logging.getLogger(__name__)


class VectorStoreError(RuntimeError):
    """Raised when the Pinecone vector store cannot be initialized or written to."""


class PineconeVectorStore:
    """Thin wrapper around Pinecone index lifecycle and vector upserts."""

    def __init__(
        self,
        *,
        api_key: str,
        index_name: str,
        dimension: int,
        metric: str,
        cloud: str,
        region: str,
    ) -> None:
        try:
            from pinecone import Pinecone, ServerlessSpec
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise VectorStoreError("pinecone is required for vector indexing.") from exc

        self._pinecone = Pinecone(api_key=api_key)
        self._serverless_spec = ServerlessSpec
        self.index_name = index_name
        self.dimension = dimension
        self.metric = metric
        self.cloud = cloud
        self.region = region
        self._index = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "PineconeVectorStore":
        """Construct a vector store from application settings."""

        return cls(
            api_key=settings.pinecone_api_key,
            index_name=settings.pinecone_index_name,
            dimension=settings.pinecone_dimension,
            metric=settings.pinecone_metric,
            cloud=settings.pinecone_cloud,
            region=settings.pinecone_region,
        )

    def ensure_index(self) -> None:
        """Create the Pinecone index if it does not already exist."""

        if not self._pinecone.has_index(self.index_name):
            LOGGER.info("Creating Pinecone index '%s'.", self.index_name)
            self._pinecone.create_index(
                name=self.index_name,
                vector_type="dense",
                dimension=self.dimension,
                metric=self.metric,
                spec=self._serverless_spec(cloud=self.cloud, region=self.region),
                deletion_protection="disabled",
            )

        self._wait_until_ready()
        self._index = self._pinecone.Index(name=self.index_name)

    def upsert(self, vectors: list[dict[str, Any]], *, namespace: str) -> int:
        """Upsert vectors into Pinecone and return the number written."""

        if self._index is None:
            self.ensure_index()

        try:
            response = self._index.upsert(vectors=vectors, namespace=namespace)
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise VectorStoreError("Failed to upsert vectors into Pinecone.") from exc

        count = self._extract_upsert_count(response)
        LOGGER.info("Upserted %s vector(s) into Pinecone namespace '%s'.", count, namespace)
        return count

    def query(
        self,
        *,
        vector: list[float],
        top_k: int,
        namespace: str,
        metadata_filter: dict[str, Any] | None = None,
        include_metadata: bool = True,
        include_values: bool = False,
    ) -> Any:
        """Query Pinecone for the nearest vectors to the given embedding."""

        if self._index is None:
            self.ensure_index()

        query_kwargs: dict[str, Any] = {
            "namespace": namespace,
            "vector": vector,
            "top_k": top_k,
            "include_metadata": include_metadata,
            "include_values": include_values,
        }
        if metadata_filter:
            query_kwargs["filter"] = metadata_filter

        try:
            return self._index.query(**query_kwargs)
        except Exception as exc:  # pragma: no cover - network/provider dependent
            raise VectorStoreError("Failed to query Pinecone.") from exc

    def _wait_until_ready(self, timeout_seconds: int = 120) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            description = self._pinecone.describe_index(self.index_name)
            ready = self._extract_ready_flag(description)
            if ready:
                self._validate_index_metadata(description)
                return
            time.sleep(2)

        raise VectorStoreError(f"Pinecone index '{self.index_name}' was not ready in time.")

    def _validate_index_metadata(self, description: Any) -> None:
        dimension = getattr(description, "dimension", None)
        metric = getattr(description, "metric", None)
        if dimension is None and isinstance(description, dict):
            dimension = description.get("dimension")
            metric = description.get("metric")

        if dimension not in (None, self.dimension):
            raise VectorStoreError(
                f"Pinecone index '{self.index_name}' has dimension {dimension}, expected {self.dimension}."
            )
        if metric not in (None, self.metric):
            raise VectorStoreError(
                f"Pinecone index '{self.index_name}' has metric {metric}, expected {self.metric}."
            )

    @staticmethod
    def _extract_ready_flag(description: Any) -> bool:
        status = getattr(description, "status", None)
        if status is None and isinstance(description, dict):
            status = description.get("status", {})

        if isinstance(status, dict):
            return bool(status.get("ready"))
        return bool(getattr(status, "ready", False))

    @staticmethod
    def _extract_upsert_count(response: Any) -> int:
        count = getattr(response, "upserted_count", None)
        if count is not None:
            return int(count)

        count = getattr(response, "upsertedCount", None)
        if count is not None:
            return int(count)

        if isinstance(response, dict):
            if "upsertedCount" in response:
                return int(response["upsertedCount"])
            if "upserted_count" in response:
                return int(response["upserted_count"])

        return 0
