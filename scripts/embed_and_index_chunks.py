"""Embed chunk JSONL files and upsert vectors into Pinecone."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError
from app.rag.ingestion.pipeline import TextbookPreprocessingPipeline


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    pipeline = TextbookPreprocessingPipeline(repo_root=PROJECT_ROOT)
    try:
        pipeline.embed_and_index_chunks()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
