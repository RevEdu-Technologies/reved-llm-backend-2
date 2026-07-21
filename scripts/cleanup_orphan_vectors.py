"""Delete orphan vectors in Pinecone whose document_id no longer matches the
current on-disk JSONL set.

The chunk_id format embeds ``document_id`` which is derived from a chunk
file's path. When the dataset directory layout changes (e.g. textbooks ->
textbook), step-N vectors keep living in the index under the OLD
document_id prefix while step-(N+1) writes a fresh set under the new
prefix. This script removes the old set.

Run from the repo root:

    python scripts/cleanup_orphan_vectors.py                  # dry-run first
    python scripts/cleanup_orphan_vectors.py --apply          # actually delete

Strategy:
  1. Read every JSONL under ``data/chunks/**`` (excluding ``*_v1_backup``)
     and collect the set of *current* document_ids.
  2. Page through every vector in the target namespace (via Pinecone's
     ``list_paginated`` / ``list``).
  3. Group vector IDs by their derived document_id prefix.
  4. Any document_id not in the current set is orphaned -> delete its
     vectors in batches.

Idempotent. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError, get_settings  # noqa: E402

LOGGER = logging.getLogger("cleanup_orphan_vectors")

# Chunk ID format: "<document_id>-<index:04d>-<content_hash[:12]>".
# document_id can itself contain hyphens, so we strip from the right:
# remove the last two hyphen-separated segments.
_INDEX_HASH_TAIL = re.compile(r"-\d{4}-[0-9a-f]{12}$")


def document_id_from_chunk_id(chunk_id: str) -> str:
    """Recover the document_id prefix from a full chunk_id."""
    return _INDEX_HASH_TAIL.sub("", chunk_id)


def collect_current_document_ids(chunks_root: Path) -> set[str]:
    """Read first record of each JSONL to extract document_id (faster than
    parsing every line; document_id is constant within a file)."""
    current: set[str] = set()
    for chunk_file in chunks_root.rglob("*.jsonl"):
        if "v1_backup" in chunk_file.as_posix():
            continue
        with chunk_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                doc_id = record.get("document_id")
                if doc_id:
                    current.add(doc_id)
                break
    return current


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete (default is dry-run).")
    parser.add_argument("--namespace", default=None, help="Override the namespace from .env.")
    parser.add_argument("--batch-size", type=int, default=200, help="Delete batch size.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    chunks_root = PROJECT_ROOT / "data" / "chunks"
    if not chunks_root.exists():
        raise SystemExit(f"No chunks tree at {chunks_root}")

    current = collect_current_document_ids(chunks_root)
    LOGGER.info("Current document_ids on disk: %s", len(current))

    try:
        from pinecone import Pinecone
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pinecone library required.") from exc

    pc = Pinecone(api_key=settings.pinecone_api_key)
    index = pc.Index(name=settings.pinecone_index_name)
    namespace = args.namespace or settings.pinecone_namespace
    LOGGER.info("Scanning namespace '%s' in index '%s'...", namespace, settings.pinecone_index_name)

    # Page through all IDs in the namespace.
    seen_ids: list[str] = []
    pagination_token: str | None = None
    while True:
        kwargs = {"namespace": namespace, "limit": 99}
        if pagination_token:
            kwargs["pagination_token"] = pagination_token
        try:
            page = index.list_paginated(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"list_paginated failed: {exc}")

        # Page shape varies by Pinecone SDK version; handle both attribute and dict.
        vectors = getattr(page, "vectors", None) or (page.get("vectors") if isinstance(page, dict) else [])
        for entry in vectors:
            vid = getattr(entry, "id", None) or (entry.get("id") if isinstance(entry, dict) else None)
            if vid:
                seen_ids.append(vid)

        pagination = getattr(page, "pagination", None) or (page.get("pagination") if isinstance(page, dict) else None)
        if pagination is None:
            break
        next_token = getattr(pagination, "next", None) or (pagination.get("next") if isinstance(pagination, dict) else None)
        if not next_token:
            break
        pagination_token = next_token

    LOGGER.info("Vectors in namespace: %s", len(seen_ids))

    # Group by document_id.
    by_doc: dict[str, list[str]] = defaultdict(list)
    for vid in seen_ids:
        doc_id = document_id_from_chunk_id(vid)
        by_doc[doc_id].append(vid)

    orphan_docs = sorted(doc for doc in by_doc if doc not in current)
    orphan_vector_count = sum(len(by_doc[d]) for d in orphan_docs)
    LOGGER.info(
        "Orphan document_ids: %s | orphan vectors: %s",
        len(orphan_docs),
        orphan_vector_count,
    )
    for doc in orphan_docs[:20]:
        LOGGER.info("  orphan: %s  (%s vectors)", doc, len(by_doc[doc]))
    if len(orphan_docs) > 20:
        LOGGER.info("  ... and %s more", len(orphan_docs) - 20)

    if not args.apply:
        LOGGER.info("Dry-run only. Re-run with --apply to delete.")
        return

    if not orphan_vector_count:
        LOGGER.info("Nothing to delete.")
        return

    deleted = 0
    for doc in orphan_docs:
        ids = by_doc[doc]
        for start in range(0, len(ids), args.batch_size):
            batch = ids[start : start + args.batch_size]
            try:
                index.delete(ids=batch, namespace=namespace)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("delete batch failed for %s (%s ids): %s", doc, len(batch), exc)
                continue
            deleted += len(batch)
            LOGGER.info("Deleted %s/%s for %s", deleted, orphan_vector_count, doc)

    LOGGER.info("Total deleted: %s", deleted)


if __name__ == "__main__":
    main()
