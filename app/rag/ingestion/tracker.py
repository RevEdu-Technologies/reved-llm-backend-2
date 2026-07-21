"""CSV-backed corpus tracking for the textbook preprocessing pipeline."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path

TRACKER_COLUMNS = [
    "file_name",
    "original_path",
    "raw_pdf_path",
    "processed_text_path",
    "chunk_output_path",
    "detected_subject",
    "content_type",
    "extraction_status",
    "chunking_status",
    "chunk_count",
    "indexing_status",
    "indexed_vector_count",
    "pinecone_namespace",
    "review_status",
    "notes",
]


@dataclass(slots=True)
class TrackerRecord:
    """Represents one row in the corpus tracker CSV."""

    file_name: str
    original_path: str
    raw_pdf_path: str = ""
    processed_text_path: str = ""
    chunk_output_path: str = ""
    detected_subject: str = ""
    content_type: str = "textbook"
    extraction_status: str = "discovered"
    chunking_status: str = ""
    chunk_count: str = ""
    indexing_status: str = ""
    indexed_vector_count: str = ""
    pinecone_namespace: str = ""
    review_status: str = ""
    notes: str = ""


class CorpusTracker:
    """Read, update, and persist the `data/corpus_tracker.csv` tracker file."""

    def __init__(self, tracker_path: Path) -> None:
        self.tracker_path = tracker_path
        self.records: dict[tuple[str, str], TrackerRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.tracker_path.exists():
            return

        with self.tracker_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                filtered = {column: row.get(column, "") for column in TRACKER_COLUMNS}
                record = TrackerRecord(**filtered)
                self.records[(record.file_name, record.original_path)] = record

    def upsert(self, record: TrackerRecord) -> None:
        self.records[(record.file_name, record.original_path)] = record

    def get(self, file_name: str, original_path: str) -> TrackerRecord | None:
        return self.records.get((file_name, original_path))

    def save(self) -> None:
        self.tracker_path.parent.mkdir(parents=True, exist_ok=True)
        with self.tracker_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRACKER_COLUMNS)
            writer.writeheader()
            sorted_records = sorted(
                self.records.values(),
                key=lambda record: (record.detected_subject, record.file_name.lower()),
            )
            for record in sorted_records:
                writer.writerow(asdict(record))
