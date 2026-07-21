"""Re-chunk every processed file across all content_types with the new chunker.

Walks ``data/processed/<content_type>/<subject>/*.txt`` and writes chunks
into ``data/chunks/<content_type>/<subject>/*.jsonl``. Each chunk record
carries ``content_type`` derived from its directory, which the source-driven
visibility lookup (``classifier.visibility_for_content_type``) turns into
``student_ok`` / ``teacher_only`` at write time.

Run from the repo root:

    python scripts/rechunk_corpus.py                       # everything
    python scripts/rechunk_corpus.py --content-type textbook
    python scripts/rechunk_corpus.py --llm                 # optional Groq pass
    python scripts/rechunk_corpus.py --dry-run

Status is tracked per file in ``data/rechunk_status.csv``. Resumable.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.ingestion.chunker import build_chunk_records, write_chunk_records  # noqa: E402
from app.rag.ingestion.organizer import CONTENT_TYPES  # noqa: E402
from app.rag.ingestion.preprocessor import strip_boilerplate  # noqa: E402

LOGGER = logging.getLogger("rechunk_corpus")

STATUS_COLUMNS = [
    "processed_txt",
    "content_type",
    "subject",
    "chunk_jsonl",
    "status",
    "chunk_count",
    "type_distribution",
    "tokens_median",
    "tokens_max",
    "elapsed_seconds",
    "last_run_at",
    "notes",
]


@dataclass(slots=True)
class StatusRow:
    processed_txt: str
    content_type: str = ""
    subject: str = ""
    chunk_jsonl: str = ""
    status: str = ""
    chunk_count: str = ""
    type_distribution: str = ""
    tokens_median: str = ""
    tokens_max: str = ""
    elapsed_seconds: str = ""
    last_run_at: str = ""
    notes: str = ""


class RechunkStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, StatusRow] = {}
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as fh:
                for raw in csv.DictReader(fh):
                    row = StatusRow(**{c: raw.get(c, "") for c in STATUS_COLUMNS})
                    self.rows[row.processed_txt] = row

    def upsert(self, row: StatusRow) -> None:
        self.rows[row.processed_txt] = row

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=STATUS_COLUMNS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: r.processed_txt.lower()):
                writer.writerow({c: getattr(row, c) for c in STATUS_COLUMNS})


def _content_type_and_subject_from(txt_path: Path, processed_root: Path) -> tuple[str, str]:
    """Derive (content_type, subject) from a processed-text path.

    Expects layout: ``processed_root/<content_type>/<subject>/<file>.txt``.
    Returns (``general``, ``general``) as a defensive fallback.
    """
    try:
        rel = txt_path.relative_to(processed_root)
    except ValueError:
        return ("general", "general")
    parts = rel.parts
    if len(parts) >= 2:
        return (parts[0].lower(), parts[1].lower())
    return ("general", "general")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--llm",
        action="store_true",
        help="After heuristic chunking, run Groq reclassification on remaining "
             "misc chunks. Slow on free tier.",
    )
    parser.add_argument(
        "--content-type",
        type=str,
        choices=CONTENT_TYPES + ("all",),
        default="all",
        help="Restrict to one content_type. Default: process everything.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not write.")
    parser.add_argument(
        "--start-after",
        type=str,
        default=None,
        help="Skip files whose TXT path sorts <= this string (resume helper).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    processed_root = PROJECT_ROOT / "data" / "processed"
    chunks_root = PROJECT_ROOT / "data" / "chunks"

    if not processed_root.exists():
        raise SystemExit(f"No processed text root at {processed_root}")

    if args.content_type == "all":
        targets = [processed_root / ct for ct in CONTENT_TYPES if (processed_root / ct).exists()]
    else:
        targets = [processed_root / args.content_type]

    txt_files: list[Path] = []
    for root in targets:
        txt_files.extend(sorted(root.rglob("*.txt")))

    if args.start_after:
        txt_files = [p for p in txt_files if p.relative_to(PROJECT_ROOT).as_posix() > args.start_after]

    if not txt_files:
        raise SystemExit(f"No processed text files found under {[str(t) for t in targets]}")

    status = RechunkStatus(PROJECT_ROOT / "data" / "rechunk_status.csv")
    LOGGER.info(
        "Re-chunking %s file(s) | content_type=%s | llm=%s | dry_run=%s",
        len(txt_files),
        args.content_type,
        args.llm,
        args.dry_run,
    )

    reclassify_misc_chunks = None
    if args.llm:
        from app.rag.ingestion.llm_classifier import (  # noqa: E402
            reclassify_misc_chunks as _reclassify,
        )
        reclassify_misc_chunks = _reclassify

    total_chunks = 0
    for txt_path in txt_files:
        relative_txt = txt_path.relative_to(PROJECT_ROOT).as_posix()
        content_type, subject = _content_type_and_subject_from(txt_path, processed_root)
        jsonl_path = chunks_root / content_type / subject / f"{txt_path.stem}.jsonl"

        t0 = time.time()
        try:
            text = txt_path.read_text(encoding="utf-8")
            stripped = strip_boilerplate(text)
            records = build_chunk_records(
                txt_path,
                stripped,
                source_root=PROJECT_ROOT / "data",
                content_type=content_type,
            )

            if args.llm and reclassify_misc_chunks and records:
                reclassify_misc_chunks(records)

            tokens = [r.token_count for r in records]
            type_dist: dict[str, int] = {}
            for r in records:
                type_dist[r.chunk_type] = type_dist.get(r.chunk_type, 0) + 1

            if not args.dry_run:
                write_chunk_records(records, jsonl_path, overwrite=True)

            elapsed = time.time() - t0
            row = StatusRow(
                processed_txt=relative_txt,
                content_type=content_type,
                subject=subject,
                chunk_jsonl=jsonl_path.relative_to(PROJECT_ROOT).as_posix(),
                status="chunked" if not args.dry_run else "dry-run",
                chunk_count=str(len(records)),
                type_distribution=" ".join(f"{k}={v}" for k, v in sorted(type_dist.items())),
                tokens_median=str(int(sorted(tokens)[len(tokens) // 2])) if tokens else "0",
                tokens_max=str(max(tokens)) if tokens else "0",
                elapsed_seconds=f"{elapsed:.2f}",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                notes="",
            )
            LOGGER.info(
                "OK %s -> %s chunks (%.1fs)",
                relative_txt,
                len(records),
                elapsed,
            )
            total_chunks += len(records)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("FAILED %s", relative_txt)
            elapsed = time.time() - t0
            row = StatusRow(
                processed_txt=relative_txt,
                content_type=content_type,
                subject=subject,
                status="failed",
                elapsed_seconds=f"{elapsed:.2f}",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                notes=str(exc),
            )

        status.upsert(row)
        if not args.dry_run:
            status.save()

    LOGGER.info(
        "Done. %s chunk(s) across %s file(s). Status: %s",
        total_chunks,
        len(txt_files),
        (PROJECT_ROOT / "data" / "rechunk_status.csv").relative_to(PROJECT_ROOT),
    )


if __name__ == "__main__":
    main()
