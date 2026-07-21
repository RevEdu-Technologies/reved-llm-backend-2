"""Ingest new PDFs from RedEd/{Textbooks,Teachers Guide,WAEC Sylabus}/ trees.

For each PDF found in any of the three source folders, this script:

  1. Infers ``content_type`` from the top-level source folder.
  2. Infers ``subject`` from the parent folder (textbooks) or filename
     (teacher guides, syllabi); falls back to ``general`` if nothing matches.
  3. Copies the PDF to ``data/raw/<content_type>/<subject>/<filename>.pdf``
     (skips if already present — idempotent).
  4. Extracts text via PyMuPDF and saves cleaned output to
     ``data/processed/<content_type>/<subject>/<filename>.txt``.
  5. Records the result in ``data/corpus_ingest_status.csv``.

Files that PyMuPDF can't extract (scanned, corrupt) are copied into the
relevant ``data/failed/<bucket>/`` directory and logged with a reason.

The script is safe to re-run. The chunking + embedding steps run separately
(scripts/rechunk_corpus.py and the Colab notebook).

Run from the repo root:

    python scripts/ingest_new_corpus.py
    python scripts/ingest_new_corpus.py --dry-run
    python scripts/ingest_new_corpus.py --source teachers-guide   # only one tree
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.ingestion.loader import (  # noqa: E402
    ExtractionDependencyError,
    PDFExtractionError,
    extract_text_from_pdf,
)
from app.rag.ingestion.organizer import (  # noqa: E402
    classify_subject_from_name,
    ensure_dataset_structure,
)
from app.rag.ingestion.preprocessor import clean_extracted_text  # noqa: E402

LOGGER = logging.getLogger("ingest_new_corpus")

# Map source-tree folder name (case-insensitive) → (content_type, source_kind).
# "source_kind" controls how we infer the subject for each file.
_SOURCE_TREES: dict[str, tuple[str, str]] = {
    "textbooks":      ("textbook",      "folder"),
    "teachers guide": ("teacher_guide", "filename"),
    "waec sylabus":   ("syllabus",      "filename"),
}

# CLI short-name → exact folder name on disk
_SOURCE_ALIASES: dict[str, str] = {
    "textbooks":      "Textbooks",
    "teachers-guide": "Teachers Guide",
    "syllabi":        "WAEC Sylabus",
}

STATUS_COLUMNS = [
    "source_path",
    "content_type",
    "subject",
    "raw_pdf_path",
    "processed_text_path",
    "status",
    "page_count",
    "text_length",
    "elapsed_seconds",
    "last_run_at",
    "notes",
]


@dataclass(slots=True)
class StatusRow:
    source_path: str
    content_type: str = ""
    subject: str = ""
    raw_pdf_path: str = ""
    processed_text_path: str = ""
    status: str = ""
    page_count: str = ""
    text_length: str = ""
    elapsed_seconds: str = ""
    last_run_at: str = ""
    notes: str = ""


class IngestStatus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: dict[str, StatusRow] = {}
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as fh:
                for raw in csv.DictReader(fh):
                    row = StatusRow(**{c: raw.get(c, "") for c in STATUS_COLUMNS})
                    self.rows[row.source_path] = row

    def get(self, source_path: str) -> StatusRow | None:
        return self.rows.get(source_path)

    def upsert(self, row: StatusRow) -> None:
        self.rows[row.source_path] = row

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=STATUS_COLUMNS)
            writer.writeheader()
            for row in sorted(self.rows.values(), key=lambda r: r.source_path.lower()):
                writer.writerow({c: getattr(row, c) for c in STATUS_COLUMNS})


def _subject_for_textbook(pdf_path: Path) -> str:
    """Subject = parent folder name, canonicalised."""
    folder_subject = (
        pdf_path.parent.name
        .replace("-", " ")
        .replace("_", " ")
        .strip()
        .lower()
    )
    canonical = {
        "physics": "physics",
        "chemistry": "chemistry",
        "biology": "biology",
        "mathematics": "mathematics",
        "english language": "english_language",
        "literature in english": "literature_in_english",
        "economics": "economics",
        "government": "government",
        "civic education": "civic_education",
        "commerce": "commerce",
        "accounting": "accounting",
        "office practice": "office_practice",
        "computer": "computer",
        "history": "history",
        "religious studies (crs irs)": "religious_studies",
        "religious studies": "religious_studies",
        "hausa": "hausa",
        "igbo": "igbo",
        "yoruba": "yoruba",
    }.get(folder_subject)
    if canonical:
        return canonical
    # Fallback: parse filename
    return classify_subject_from_name(pdf_path.name) or "general"


def _subject_for_filename(pdf_path: Path) -> str:
    """Subject = parsed from filename only (teacher guides + syllabi)."""
    return classify_subject_from_name(pdf_path.name) or "general"


def _move_to_failed(pdf_path: Path, bucket: str, repo_root: Path) -> Path:
    dest_dir = repo_root / "data" / "failed" / bucket
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pdf_path.name
    if not dest.exists():
        shutil.copy2(pdf_path, dest)
    return dest


def _process_single(
    pdf_path: Path,
    *,
    content_type: str,
    subject: str,
    repo_root: Path,
    dry_run: bool,
) -> StatusRow:
    """Copy + extract + clean one PDF. Returns the resulting status row."""

    source_rel = pdf_path.resolve().as_posix()
    raw_dir = repo_root / "data" / "raw" / content_type / subject
    processed_dir = repo_root / "data" / "processed" / content_type / subject

    raw_pdf_path = raw_dir / pdf_path.name
    processed_text_path = processed_dir / f"{pdf_path.stem}.txt"

    row = StatusRow(
        source_path=source_rel,
        content_type=content_type,
        subject=subject,
        raw_pdf_path=str(raw_pdf_path.relative_to(repo_root)).replace("\\", "/"),
        processed_text_path=str(processed_text_path.relative_to(repo_root)).replace("\\", "/"),
        last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    if dry_run:
        row.status = "dry-run"
        row.notes = f"Would copy and extract {pdf_path.name}."
        return row

    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    if not raw_pdf_path.exists():
        shutil.copy2(pdf_path, raw_pdf_path)

    if processed_text_path.exists():
        existing_text = processed_text_path.read_text(encoding="utf-8")
        row.status = "processed"
        row.text_length = str(len(existing_text))
        row.notes = "Already processed."
        return row

    t0 = time.time()
    try:
        extraction = extract_text_from_pdf(raw_pdf_path)
    except ExtractionDependencyError:
        raise
    except PDFExtractionError as exc:
        failed_copy = _move_to_failed(raw_pdf_path, "corrupt_files", repo_root)
        row.status = "failed_extraction"
        row.notes = f"{exc}. Copied to {failed_copy.relative_to(repo_root)}"
        return row

    if extraction.low_quality:
        bucket = "scanned_pdfs" if "scanned" in (extraction.low_quality_reason or "").lower() else "low_quality_extraction"
        failed_copy = _move_to_failed(raw_pdf_path, bucket, repo_root)
        row.status = "needs_ocr"
        row.notes = f"{extraction.low_quality_reason} Copied to {failed_copy.relative_to(repo_root)}"
        return row

    cleaned = clean_extracted_text(extraction.text)
    processed_text_path.write_text(cleaned, encoding="utf-8")

    row.status = "processed"
    row.page_count = str(extraction.page_count)
    row.text_length = str(len(cleaned))
    row.elapsed_seconds = f"{time.time() - t0:.2f}"
    row.notes = f"Extracted {extraction.page_count} pages."
    return row


def discover_source_pdfs(
    reded_root: Path,
    *,
    only_source: str | None = None,
) -> list[tuple[Path, str, str]]:
    """Return ``(pdf_path, content_type, subject)`` for every relevant PDF.

    Iterates the three known source trees. Missing trees are skipped silently;
    empty trees produce zero results without erroring.
    """

    discovered: list[tuple[Path, str, str]] = []

    for tree_name, exact_name in _SOURCE_ALIASES.items():
        if only_source and only_source != tree_name:
            continue
        tree_root = reded_root / exact_name
        if not tree_root.exists():
            LOGGER.warning("Source tree missing: %s", tree_root)
            continue

        content_type, source_kind = _SOURCE_TREES[exact_name.lower()]
        for pdf_path in sorted(tree_root.rglob("*.pdf")):
            if source_kind == "folder":
                subject = _subject_for_textbook(pdf_path)
            else:
                subject = _subject_for_filename(pdf_path)
            discovered.append((pdf_path, content_type, subject))

    return discovered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=str,
        choices=list(_SOURCE_ALIASES.keys()) + ["all"],
        default="all",
        help="Restrict to one source tree.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not copy or extract.")
    parser.add_argument(
        "--reded-root",
        type=str,
        default=str(PROJECT_ROOT.parent),
        help="Root of the RedEd source tree (default: parent of repo).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    reded_root = Path(args.reded_root).resolve()
    LOGGER.info("Discovering PDFs under %s", reded_root)

    ensure_dataset_structure(PROJECT_ROOT / "data")

    only_source = None if args.source == "all" else args.source
    pdfs = discover_source_pdfs(reded_root, only_source=only_source)
    LOGGER.info("Discovered %s PDF(s)", len(pdfs))

    status = IngestStatus(PROJECT_ROOT / "data" / "corpus_ingest_status.csv")

    counts: dict[str, int] = {}
    for pdf_path, content_type, subject in pdfs:
        try:
            row = _process_single(
                pdf_path,
                content_type=content_type,
                subject=subject,
                repo_root=PROJECT_ROOT,
                dry_run=args.dry_run,
            )
        except ExtractionDependencyError:
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Unexpected failure for %s", pdf_path)
            row = StatusRow(
                source_path=pdf_path.resolve().as_posix(),
                content_type=content_type,
                subject=subject,
                status="failed",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                notes=f"{type(exc).__name__}: {exc}",
            )

        status.upsert(row)
        counts[row.status] = counts.get(row.status, 0) + 1
        LOGGER.info(
            "%-18s %-12s %-22s %s",
            row.status,
            content_type,
            subject,
            pdf_path.name,
        )

    if not args.dry_run:
        status.save()

    LOGGER.info("Done. Status counts: %s", counts)


# Expose ensure_dataset_structure to the main flow so the layout exists.
from app.rag.ingestion.organizer import ensure_dataset_structure  # noqa: E402,F811


if __name__ == "__main__":
    main()
