"""Query the Pinecone-backed textbook retriever from the command line."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError, get_settings
from app.rag.retrieval.retriever import PineconeRetriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Test semantic retrieval over the textbook corpus.")
    parser.add_argument("--query", required=True, help="Plain text query to search for.")
    parser.add_argument("--top-k", type=int, default=5, help="Number of matches to return.")
    parser.add_argument("--subject", help="Optional subject filter: physics, chemistry, or biology.")
    parser.add_argument("--namespace", help="Optional Pinecone namespace override.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        settings = get_settings()
        retriever = PineconeRetriever.from_settings(settings, repo_root=PROJECT_ROOT)
        results = retriever.retrieve(
            args.query,
            top_k=args.top_k,
            subject=args.subject,
            namespace=args.namespace,
        )
    except (ConfigurationError, ValueError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    if not results:
        print("No matches found.")
        return

    for rank, result in enumerate(results, start=1):
        preview = " ".join(result.text.split())[:220]
        print(
            f"{rank}. score={result.score:.4f} | subject={result.subject} | "
            f"source={result.source_file} | chunk_index={result.chunk_index}"
        )
        print(f"   chunk_id={result.chunk_id}")
        print(f"   document_id={result.document_id}")
        print(f"   preview={preview}")
        print()


if __name__ == "__main__":
    main()
