"""Run grounded textbook QA from the command line."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError, get_settings
from app.llm.client import LLMClientError
from app.rag.query_engine.engine import GroundedQAEngine


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Test grounded answer generation over the textbook corpus.")
    parser.add_argument("--query", required=True, help="Plain text question to answer.")
    parser.add_argument("--top-k", type=int, default=5, help="Number of retrieved chunks to use.")
    parser.add_argument("--subject", help="Optional subject filter: physics, chemistry, or biology.")
    parser.add_argument("--student-class", default="JSS1", help="Student class level, e.g. Primary 4, JSS1, SS2.")
    parser.add_argument("--namespace", help="Optional Pinecone namespace override.")
    parser.add_argument("--debug", action="store_true", help="Print debug information such as sources and retrieval preview.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        settings = get_settings()
        engine = GroundedQAEngine.from_settings(settings, repo_root=PROJECT_ROOT)
        result = engine.answer_question(
            args.query,
            top_k=args.top_k,
            subject=args.subject,
            student_class=args.student_class,
            namespace=args.namespace,
        )
    except (ConfigurationError, ValueError, LLMClientError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    print(f"Query: {args.query}")
    print(f"Student Class: {args.student_class}")
    print()
    print("Answer:")
    print(result.answer)

    if args.debug:
        print()
        print("Debug:")
        print("Sources:")
        for index, source in enumerate(result.sources, start=1):
            print(
                f"{index}. {source.source_file} | subject={source.subject} | "
                f"chunk_index={source.chunk_index} | chunk_id={source.chunk_id}"
            )
        print()
        print("Retrieved Chunks:")
        for index, chunk in enumerate(result.retrieved_chunks, start=1):
            preview = " ".join(chunk.text.split())[:180]
            print(
                f"{index}. score={chunk.score:.4f} | subject={chunk.subject} | "
                f"source={chunk.source_file} | chunk_index={chunk.chunk_index}"
            )
            print(f"   preview={preview}")
            print()


if __name__ == "__main__":
    main()
