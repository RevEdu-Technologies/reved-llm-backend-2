"""PDF loading and text extraction utilities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ExtractionDependencyError(RuntimeError):
    """Raised when the PDF extraction dependency is unavailable."""


class PDFExtractionError(RuntimeError):
    """Raised when a PDF cannot be extracted safely."""


@dataclass(slots=True)
class ExtractionResult:
    """Text extracted from a PDF plus lightweight quality metadata."""

    text: str
    page_count: int
    text_length: int
    low_quality: bool
    low_quality_reason: str | None = None


def extract_text_from_pdf(pdf_path: Path) -> ExtractionResult:
    """Extract text from a PDF with PyMuPDF.

    Parameters
    ----------
    pdf_path:
        Path to the PDF that should be read.
    """

    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ExtractionDependencyError(
            "PyMuPDF is required for PDF extraction. Install the 'PyMuPDF' "
            "package before running the preprocessing pipeline."
        ) from exc

    try:
        document = fitz.open(pdf_path)
    except Exception as exc:  # pragma: no cover - PyMuPDF exception types vary
        raise PDFExtractionError(f"Unable to open PDF: {pdf_path}") from exc

    try:
        page_text: list[str] = []
        for page in document:
            extracted = page.get_text("text")
            page_text.append(extracted or "")
    except Exception as exc:  # pragma: no cover - PyMuPDF exception types vary
        raise PDFExtractionError(f"Unable to extract text from PDF: {pdf_path}") from exc
    finally:
        document.close()

    text = "\n".join(page_text)
    compact_text = "".join(text.split())
    low_quality_reason: str | None = None
    low_quality = False

    if not compact_text:
        low_quality = True
        low_quality_reason = "No extractable text found; likely scanned PDF."
    elif len(compact_text) < 120:
        low_quality = True
        low_quality_reason = "Extracted text is unusually short."

    return ExtractionResult(
        text=text,
        page_count=len(page_text),
        text_length=len(text),
        low_quality=low_quality,
        low_quality_reason=low_quality_reason,
    )
