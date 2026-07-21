"""Backfill `text` into Pinecone vector metadata for the existing v2 index.

When the v2 re-embed ran, PINECONE_INCLUDE_CHUNK_TEXT was False, so vectors
went up with metadata that omitted the chunk text. The retriever then had to
fall back to reading local JSONLs from disk to hydrate result text — which
ties retrieval to the local filesystem layout.

This script walks every chunk JSONL on disk and calls Pinecone's
``update(id=..., set_metadata={...})`` to add ``text`` (and refresh the
existing fields, harmlessly) to each vector that already exists in the
index. It does NOT re-embed anything — vector values are untouched.

Run from the repo root after .env has PINECONE_INCLUDE_CHUNK_TEXT=true:

    python scripts/backfill_chunk_text_metadata.py

Optional flags:
    --dry-run           Don't write anything; just log what would change.
    --limit N           Stop after N vectors (handy for a quick smoke test).
    --start-after PATH  Skip JSONL files alphabetically before this path
                        (handy for resuming).

Resumable: progress is written to ``data/backfill_metadata_status.csv`` after
each chunk file. Already-completed files are skipped on subsequent runs.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import ConfigurationError, get_settings  # noqa: E402

LOGGER = logging.getLogger("backfill_chunk_text_metadata")

STATUS_COLUMNS = [
    "chunk_file",
    "subject",
    "source_file",
    "status",
    "updated_count",
    "failed_count",
    "pinecone_index",
    "pinecone_namespace",
    "last_run_at",
    "notes",
]


@dataclass(slots=True)
class StatusRow:
    chunk_file: str
    subject: str = ""
    source_file: str = ""
    status: str = ""
    updated_count: str = ""
    failed_count: str = ""
    pinecone_index: str = ""
    pinecone_namespace: str = ""
    last_run_at: str = ""
    notes: str = ""


class BackfillStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, StatusRow] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                row = StatusRow(**{c: raw.get(c, "") for c in STATUS_COLUMNS})
                self.rows[row.chunk_file] = row

    def get(self, chunk_file: str) -> StatusRow | None:
        return self.rows.get(chunk_file)

    def upsert(self, row: StatusRow) -> None:
        self.rows[row.chunk_file] = row

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=STATUS_COLUMNS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: r.chunk_file.lower()):
                writer.writerow({c: getattr(row, c) for c in STATUS_COLUMNS})


def _build_metadata(record: dict) -> dict:
    return {
        "source_file": record["source_file"],
        "source_path": record["source_path"],
        "subject": record["subject"],
        "content_type": record["content_type"],
        "chunk_index": int(record["chunk_index"]),
        "total_chunks": int(record["total_chunks"]),
        "document_id": record["document_id"],
        "char_count": int(record["char_count"]),
        "text": record["text"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't call Pinecone; just log.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N vector updates.")
    parser.add_argument(
        "--start-after",
        type=str,
        default=None,
        help="Resume by skipping files alphabetically <= this relative path.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    chunk_root = PROJECT_ROOT / "data" / "chunks" / "textbook"
    chunk_files = sorted(chunk_root.rglob("*.jsonl"))
    if not chunk_files:
        raise SystemExit(f"No JSONL chunk files found under {chunk_root}.")

    if args.start_after:
        chunk_files = [p for p in chunk_files if p.relative_to(PROJECT_ROOT).as_posix() > args.start_after]

    status = BackfillStatus(PROJECT_ROOT / "data" / "backfill_metadata_status.csv")

    try:
        from pinecone import Pinecone
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pinecone is required.") from exc

    pc = Pinecone(api_key=settings.pinecone_api_key)
    index = pc.Index(name=settings.pinecone_index_name)
    namespace = settings.pinecone_namespace

    LOGGER.info(
        "Backfill plan: %s file(s) | index=%s | namespace=%s | dry_run=%s",
        len(chunk_files),
        settings.pinecone_index_name,
        namespace,
        args.dry_run,
    )

    total_updated = 0
    for chunk_file in chunk_files:
        rel = chunk_file.relative_to(PROJECT_ROOT).as_posix()
        prior = status.get(rel)
        if (
            prior
            and prior.status == "updated"
            and prior.pinecone_index == settings.pinecone_index_name
        ):
            LOGGER.info("Skipping already backfilled: %s (%s updated)", rel, prior.updated_count)
            total_updated += int(prior.updated_count or 0)
            continue

        records: list[dict] = []
        with chunk_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    records.append(json.loads(stripped))
        if not records:
            continue

        subject = chunk_file.parent.name.lower()
        source_file = records[0].get("source_file", f"{chunk_file.stem}.txt")
        LOGGER.info("Backfilling %s (%s vectors)", rel, len(records))

        updated = 0
        failed = 0
        t0 = time.time()
        for record in records:
            if args.limit is not None and total_updated + updated >= args.limit:
                break
            metadata = _build_metadata(record)
            if args.dry_run:
                updated += 1
                continue
            try:
                index.update(
                    id=record["chunk_id"],
                    set_metadata=metadata,
                    namespace=namespace,
                )
                updated += 1
            except Exception as exc:  # noqa: BLE001 — record + continue
                LOGGER.warning("Update failed for %s: %s", record.get("chunk_id"), exc)
                failed += 1

        elapsed = time.time() - t0
        LOGGER.info(
            "  done in %.1fs: updated=%s failed=%s",
            elapsed,
            updated,
            failed,
        )

        status.upsert(
            StatusRow(
                chunk_file=rel,
                subject=subject,
                source_file=source_file,
                status="updated" if failed == 0 else "partial",
                updated_count=str(updated),
                failed_count=str(failed),
                pinecone_index=settings.pinecone_index_name,
                pinecone_namespace=namespace,
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                notes=f"Updated {updated} of {len(records)} vector(s); {failed} failed.",
            )
        )
        if not args.dry_run:
            status.save()
        total_updated += updated

        if args.limit is not None and total_updated >= args.limit:
            LOGGER.info("Hit --limit %s, stopping.", args.limit)
            break

    LOGGER.info(
        "Backfill complete. %s vector metadata records updated across %s file(s).",
        total_updated,
        len(chunk_files),
    )


if __name__ == "__main__":
    main()
