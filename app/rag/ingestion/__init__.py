"""Dataset organization and preprocessing helpers for textbook ingestion."""

from .chunker import ChunkRecord, ChunkingConfig, build_chunk_records, chunk_text
from .loader import extract_text_from_pdf
from .organizer import (
    SUBJECTS,
    classify_subject_from_name,
    discover_root_pdfs,
    ensure_dataset_structure,
    organize_pdf_copy,
)
from .pipeline import TextbookPreprocessingPipeline
from .preprocessor import clean_extracted_text
from .tracker import CorpusTracker, TrackerRecord

__all__ = [
    "CorpusTracker",
    "ChunkRecord",
    "ChunkingConfig",
    "SUBJECTS",
    "TextbookPreprocessingPipeline",
    "TrackerRecord",
    "build_chunk_records",
    "classify_subject_from_name",
    "clean_extracted_text",
    "chunk_text",
    "discover_root_pdfs",
    "ensure_dataset_structure",
    "extract_text_from_pdf",
    "organize_pdf_copy",
]
