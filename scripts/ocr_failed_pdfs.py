"""OCR the scanned PDFs parked in ``data/failed/scanned_pdfs/``.

For each PDF flagged ``needs_ocr`` in ``data/corpus_ingest_status.csv``:

  1. Convert each page to an image via Poppler (``pdf2image``).
  2. Run Tesseract OCR on each page image.
  3. Concatenate, clean with the same preprocessor used for native PDFs.
  4. Write the result to the row's ``processed_text_path``.
  5. Update the status row to ``processed`` so the rechunker picks it up.

Requirements:
  * Tesseract 5.x on PATH (``tesseract --version``).
  * Poppler with ``pdftoppm`` on PATH (``pdftoppm -v``).
  * Python packages: ``pytesseract``, ``pdf2image``.

Run from the repo root:

    python scripts/ocr_failed_pdfs.py                       # all queued
    python scripts/ocr_failed_pdfs.py --limit 1             # just the first
    python scripts/ocr_failed_pdfs.py --only-file "Wren"    # filename filter
    python scripts/ocr_failed_pdfs.py --dpi 250 --tess-lang eng

Resumable: a PDF whose processed_text_path already exists is skipped.

Page images are written to a temp folder during OCR (so memory stays flat
on long books) and deleted afterward.
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.ingestion.preprocessor import clean_extracted_text  # noqa: E402

LOGGER = logging.getLogger("ocr_failed_pdfs")


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


def _load_status(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _save_status(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=STATUS_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in STATUS_COLUMNS})


def _ocr_one_pdf(
    pdf_path: Path,
    *,
    dpi: int,
    tess_lang: str,
    poppler_path: str | None,
    tesseract_cmd: str | None = None,
) -> tuple[str, int]:
    """Return ``(joined_text, page_count)`` from OCR'ing ``pdf_path``.

    Images are rendered to a TemporaryDirectory so we don't hold them in RAM
    on long books. ``pytesseract`` is invoked per page so we can log progress
    on big PDFs without waiting for the whole job to finish.
    """

    from pdf2image import convert_from_path
    import pytesseract

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    page_texts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="reved_ocr_") as tmp:
        images = convert_from_path(
            str(pdf_path),
            dpi=dpi,
            output_folder=tmp,
            poppler_path=poppler_path,
            paths_only=True,
            fmt="png",
        )
        page_count = len(images)
        for idx, image_path in enumerate(images, start=1):
            if idx == 1 or idx % 25 == 0 or idx == page_count:
                LOGGER.info("  page %s/%s", idx, page_count)
            try:
                text = pytesseract.image_to_string(image_path, lang=tess_lang)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("  page %s OCR failed: %s", idx, exc)
                continue
            if text.strip():
                page_texts.append(text)

    joined = "\n\n".join(page_texts)
    return joined, page_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dpi", type=int, default=200, help="Render DPI. Default 200.")
    parser.add_argument("--tess-lang", default="eng", help="Tesseract language code. Default 'eng'.")
    parser.add_argument(
        "--poppler-path",
        default=None,
        help="Path to Poppler bin dir (if not on PATH). Optional.",
    )
    parser.add_argument(
        "--tesseract-cmd",
        default=None,
        help="Full path to tesseract.exe (if not on PATH). Optional.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process at most N PDFs.")
    parser.add_argument(
        "--only-file",
        default=None,
        help="Case-insensitive substring filter on the source PDF filename.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-OCR even if processed_text_path already exists.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    status_path = PROJECT_ROOT / "data" / "corpus_ingest_status.csv"
    if not status_path.exists():
        raise SystemExit(f"Status CSV not found: {status_path}")

    rows = _load_status(status_path)
    # Retry both never-attempted and previously-failed rows. A real OCR success
    # flips the row to "processed"; we never re-process that state unless the
    # user passes --force.
    retry_states = {"needs_ocr", "failed_ocr"}
    queue = [r for r in rows if r.get("status") in retry_states]
    if args.only_file:
        needle = args.only_file.lower()
        queue = [r for r in queue if needle in r["source_path"].lower()]
    if args.limit is not None:
        queue = queue[: args.limit]

    LOGGER.info(
        "OCR plan: %s file(s) | dpi=%s | lang=%s | force=%s",
        len(queue),
        args.dpi,
        args.tess_lang,
        args.force,
    )
    if not queue:
        LOGGER.info("Nothing queued. Done.")
        return

    failed_dir = PROJECT_ROOT / "data" / "failed" / "scanned_pdfs"
    processed_count = 0

    for row in queue:
        source_basename = Path(row["source_path"]).name
        pdf_in_failed = failed_dir / source_basename
        if not pdf_in_failed.exists():
            # Fall back to the original RedEd source path
            raw_candidate = PROJECT_ROOT / row.get("raw_pdf_path", "")
            if raw_candidate.exists():
                pdf_in_failed = raw_candidate
            else:
                LOGGER.warning("SKIP %s: source PDF not found", source_basename)
                continue

        processed_text_path = PROJECT_ROOT / row["processed_text_path"]
        if processed_text_path.exists() and not args.force:
            LOGGER.info("SKIP %s: already processed at %s", source_basename, processed_text_path)
            row["status"] = "processed"
            row["notes"] = (row.get("notes") or "") + " | OCR skipped: text already exists."
            processed_count += 1
            continue

        LOGGER.info(
            "OCR %s -> %s/%s",
            source_basename,
            row["content_type"],
            row["subject"],
        )

        t0 = time.time()
        try:
            raw_text, page_count = _ocr_one_pdf(
                pdf_in_failed,
                dpi=args.dpi,
                tess_lang=args.tess_lang,
                poppler_path=args.poppler_path,
                tesseract_cmd=args.tesseract_cmd,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("FAIL %s", source_basename)
            row["status"] = "failed_ocr"
            row["last_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row["notes"] = f"OCR failed: {type(exc).__name__}: {exc}"
            _save_status(status_path, rows)
            continue

        if not raw_text.strip():
            row["status"] = "failed_ocr"
            row["last_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row["notes"] = f"OCR produced empty text after {page_count} page(s)."
            LOGGER.warning("FAIL %s: empty OCR output", source_basename)
            _save_status(status_path, rows)
            continue

        cleaned = clean_extracted_text(raw_text)
        processed_text_path.parent.mkdir(parents=True, exist_ok=True)
        processed_text_path.write_text(cleaned, encoding="utf-8")

        elapsed = time.time() - t0
        row["status"] = "processed"
        row["page_count"] = str(page_count)
        row["text_length"] = str(len(cleaned))
        row["elapsed_seconds"] = f"{elapsed:.1f}"
        row["last_run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row["notes"] = f"OCR by tesseract (dpi={args.dpi}, lang={args.tess_lang})."
        _save_status(status_path, rows)

        # Move the source out of failed/ so reruns of ingest don't re-flag it.
        recovered_dir = PROJECT_ROOT / "data" / "failed" / "ocr_recovered"
        recovered_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(pdf_in_failed), str(recovered_dir / pdf_in_failed.name))
        except Exception:  # noqa: BLE001
            pass  # non-fatal — keep the source where it was

        LOGGER.info(
            "DONE %s (%s pages, %s chars, %.1fs)",
            source_basename,
            page_count,
            len(cleaned),
            elapsed,
        )
        processed_count += 1

    _save_status(status_path, rows)
    LOGGER.info("OCR run complete. %s file(s) recovered.", processed_count)


if __name__ == "__main__":
    main()
