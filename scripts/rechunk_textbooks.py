"""Re-chunk every processed textbook with the new structure-aware chunker.

Pipeline per file:
  1. Read ``data/processed/textbooks/<subject>/<file>.txt``.
  2. Run ``strip_boilerplate`` to drop TOC dot-leader lines + copyright/ISBN/
     license front-matter.
  3. Run ``build_chunk_records`` (token-based, heading-aware, with quality
     filtering and source-driven visibility).
  4. (Optional) Run ``reclassify_misc_chunks`` over chunks still tagged
     ``misc``. Off by default — Groq free tier is slow at this volume; the
     heuristic now defaults to ``explanation`` so the LLM pass is polish.
  5. Write to ``data/chunks/textbooks/<subject>/<file>.jsonl``.

Existing JSONLs are first **moved** to
``data/chunks/textbooks_v1_backup/<subject>/`` so we can roll back or A/B
compare. The backup directory is created on the first run; subsequent runs
skip files already backed up.

Run from the repo root:

    python scripts/rechunk_textbooks.py             # heuristic only (fast)
    python scripts/rechunk_textbooks.py --llm       # add Groq reclassification
    python scripts/rechunk_textbooks.py --dry-run   # plan-only; no writes

A status CSV is written to ``data/rechunk_status.csv`` so the run is
auditable and resumable.
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.ingestion.chunker import build_chunk_records, write_chunk_records  # noqa: E402
from app.rag.ingestion.preprocessor import strip_boilerplate  # noqa: E402

LOGGER = logging.getLogger("rechunk_textbooks")

STATUS_COLUMNS = [
    "processed_txt",
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


def _backup_existing_jsonls(chunks_root: Path, backup_root: Path, dry_run: bool) -> int:
    """Move existing chunk JSONLs into the backup root. Idempotent."""
    moved = 0
    if not chunks_root.exists():
        return 0
    for jsonl in chunks_root.rglob("*.jsonl"):
        relative = jsonl.relative_to(chunks_root)
        target = backup_root / relative
        if target.exists():
            continue
        if dry_run:
            LOGGER.info("[dry-run] would back up %s -> %s", jsonl, target)
            moved += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(jsonl), str(target))
        LOGGER.info("Backed up %s -> %s", jsonl, target)
        moved += 1
    return moved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--llm",
        action="store_true",
        help="After heuristic chunking, run Groq reclassification on any "
             "remaining misc chunks. Slow on free tier (~2-3 hours full corpus).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not write.")
    parser.add_argument(
        "--start-after",
        type=str,
        default=None,
        help="Skip files whose relative TXT path sorts <= this string (resume helper).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    processed_root = PROJECT_ROOT / "data" / "processed" / "textbooks"
    chunks_root = PROJECT_ROOT / "data" / "chunks" / "textbooks"
    backup_root = PROJECT_ROOT / "data" / "chunks" / "textbooks_v1_backup"

    if not processed_root.exists():
        raise SystemExit(f"No processed text found at {processed_root}")

    LOGGER.info("Backing up existing JSONLs to %s", backup_root)
    _backup_existing_jsonls(chunks_root, backup_root, args.dry_run)

    status = RechunkStatus(PROJECT_ROOT / "data" / "rechunk_status.csv")
    txt_files = sorted(processed_root.rglob("*.txt"))

    if args.start_after:
        txt_files = [p for p in txt_files if p.relative_to(PROJECT_ROOT).as_posix() > args.start_after]

    LOGGER.info(
        "Re-chunking %s file(s) | llm=%s | dry_run=%s",
        len(txt_files),
        args.llm,
        args.dry_run,
    )

    # Lazy import so dry-run / heuristic-only runs don't require LLM client setup.
    reclassify_misc_chunks = None
    if args.llm:
        from app.rag.ingestion.llm_classifier import (  # noqa: E402
            reclassify_misc_chunks as _reclassify,
        )
        reclassify_misc_chunks = _reclassify

    total_chunks = 0
    for txt_path in txt_files:
        relative_txt = txt_path.relative_to(PROJECT_ROOT).as_posix()
        subject = txt_path.parent.name.lower()
        jsonl_path = chunks_root / subject / f"{txt_path.stem}.jsonl"

        t0 = time.time()
        try:
            text = txt_path.read_text(encoding="utf-8")
            stripped = strip_boilerplate(text)
            records = build_chunk_records(
                txt_path,
                stripped,
                source_root=PROJECT_ROOT / "data",
                content_type="textbook",
            )

            if args.llm and reclassify_misc_chunks and records:
                reclassify_misc_chunks(records)

            tokens = [r.token_count for r in records]
            type_dist = {}
            for r in records:
                type_dist[r.chunk_type] = type_dist.get(r.chunk_type, 0) + 1

            if not args.dry_run:
                write_chunk_records(records, jsonl_path, overwrite=True)

            elapsed = time.time() - t0
            row = StatusRow(
                processed_txt=relative_txt,
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
        except Exception as exc:  # noqa: BLE001 — want a status row even on failure
            LOGGER.exception("FAILED %s", relative_txt)
            elapsed = time.time() - t0
            row = StatusRow(
                processed_txt=relative_txt,
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
