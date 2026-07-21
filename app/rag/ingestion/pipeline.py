"""Pipeline orchestration for the MVP textbook dataset preprocessing flow."""

from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path

from .chunker import ChunkingConfig, build_chunk_records, write_chunk_records
from .loader import ExtractionDependencyError, PDFExtractionError, extract_text_from_pdf
from .organizer import (
    classify_subject_from_name,
    discover_root_pdfs,
    ensure_dataset_structure,
    organize_pdf_copy,
)
from .preprocessor import clean_extracted_text
from .tracker import CorpusTracker, TrackerRecord

LOGGER = logging.getLogger(__name__)


class TextbookPreprocessingPipeline:
    """Discover, organize, extract, clean, and track textbook PDFs."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
        self.data_dir = self.repo_root / "data"
        self.raw_dir = self.data_dir / "raw" / "textbook"
        self.processed_dir = self.data_dir / "processed" / "textbook"
        self.chunk_dir = self.data_dir / "chunks" / "textbook"
        self.failed_dir = self.data_dir / "failed"
        self.tracker = CorpusTracker(self.data_dir / "corpus_tracker.csv")
        ensure_dataset_structure(self.data_dir)

    def discover(self) -> list[Path]:
        """Discover PDFs in the repository root."""

        pdfs = discover_root_pdfs(self.repo_root)
        LOGGER.info("Discovered %s root PDF(s).", len(pdfs))
        return pdfs

    def organize(self, preserve_original: bool = True) -> list[TrackerRecord]:
        """Classify and organize root PDFs into raw subject folders."""

        records: list[TrackerRecord] = []
        for pdf_path in self.discover():
            record = self._build_base_record(pdf_path)
            subject = classify_subject_from_name(pdf_path.name)
            record.detected_subject = subject or ""

            if not subject:
                record.extraction_status = "discovered"
                record.review_status = "needs_manual_review"
                record.notes = "Filename heuristic could not determine subject."
                self.tracker.upsert(record)
                records.append(record)
                continue

            destination_dir = self.raw_dir / subject
            raw_pdf_path = organize_pdf_copy(pdf_path, destination_dir)
            if not preserve_original:
                LOGGER.warning(
                    "preserve_original=False requested, but the MVP pipeline always keeps "
                    "the original file in place. Raw copy written to %s",
                    raw_pdf_path,
                )

            record.raw_pdf_path = self._relative(raw_pdf_path)
            record.extraction_status = "organized"
            record.review_status = "auto_classified"
            record.notes = "Copied from repository root into raw textbook dataset."
            self.tracker.upsert(record)
            records.append(record)

        self.tracker.save()
        return records

    def embed_and_index_chunks(self) -> list[TrackerRecord]:
        """Embed chunk JSONL files and upsert them into Pinecone."""

        from app.core.config import get_settings
        from app.rag.embedding import get_embedder
        from app.rag.vectorstore.indexer import ChunkVectorIndexer
        from app.rag.vectorstore.store import PineconeVectorStore

        settings = get_settings()
        embedder = get_embedder(settings)
        vector_store = PineconeVectorStore.from_settings(settings)
        vector_store.ensure_index()
        indexer = ChunkVectorIndexer(
            embedder=embedder,
            vector_store=vector_store,
            namespace=settings.pinecone_namespace,
            embedding_batch_size=settings.hf_embedding_batch_size,
            upsert_batch_size=settings.pinecone_upsert_batch_size,
            include_chunk_text=settings.pinecone_include_chunk_text,
        )

        records: list[TrackerRecord] = []
        for chunk_file in sorted(self.chunk_dir.rglob("*.jsonl")):
            tracker_record = self._record_for_chunk_file(chunk_file)
            if tracker_record.indexing_status == "indexed":
                LOGGER.info("Skipping already indexed chunk file: %s", chunk_file)
                records.append(tracker_record)
                continue

            result = indexer.index_chunk_file(chunk_file)
            updated = replace(
                tracker_record,
                indexing_status=result.status,
                indexed_vector_count=str(result.indexed_count),
                pinecone_namespace=result.namespace,
                notes=self._merge_notes(tracker_record.notes, result.notes),
            )
            self.tracker.upsert(updated)
            self.tracker.save()
            records.append(updated)

        return records

    def process(self) -> list[TrackerRecord]:
        """Run the full organization and preprocessing flow."""

        organized_records = self.organize()
        processed_records: list[TrackerRecord] = []

        for record in organized_records:
            if record.review_status == "needs_manual_review":
                processed_records.append(record)
                continue

            if not record.raw_pdf_path:
                processed_records.append(record)
                continue

            raw_pdf_path = self.repo_root / record.raw_pdf_path
            current = self.tracker.get(record.file_name, record.original_path) or record
            updated = self._process_single_pdf(current, raw_pdf_path)
            self.tracker.upsert(updated)
            processed_records.append(updated)

        self.tracker.save()
        return processed_records

    def chunk_processed_texts(
        self,
        *,
        config: ChunkingConfig | None = None,
        overwrite: bool = True,
    ) -> list[TrackerRecord]:
        """Chunk processed textbook text files into JSONL outputs."""

        config = config or ChunkingConfig()
        records: list[TrackerRecord] = []

        for processed_text_path in sorted(self.processed_dir.rglob("*.txt")):
            record = self._record_for_processed_file(processed_text_path)
            updated = self._chunk_single_processed_text(
                record,
                processed_text_path,
                config=config,
                overwrite=overwrite,
            )
            self.tracker.upsert(updated)
            records.append(updated)

        self.tracker.save()
        return records

    def _process_single_pdf(self, record: TrackerRecord, raw_pdf_path: Path) -> TrackerRecord:
        subject = record.detected_subject
        if not subject:
            return record

        try:
            extraction = extract_text_from_pdf(raw_pdf_path)
        except ExtractionDependencyError:
            raise
        except PDFExtractionError as exc:
            failed_copy = self._copy_to_failed(raw_pdf_path, "corrupt_files")
            return replace(
                record,
                extraction_status="failed_extraction",
                review_status="failed_extraction",
                notes=f"{exc}. Copied to {self._relative(failed_copy)}",
            )

        if extraction.low_quality:
            failed_bucket = "scanned_pdfs" if "scanned" in (extraction.low_quality_reason or "").lower() else "low_quality_extraction"
            failed_copy = self._copy_to_failed(raw_pdf_path, failed_bucket)
            return replace(
                record,
                extraction_status="needs_ocr",
                review_status="needs_manual_review",
                notes=f"{extraction.low_quality_reason} Copied to {self._relative(failed_copy)}",
            )

        cleaned_text = clean_extracted_text(extraction.text)
        processed_text_path = self.processed_dir / subject / f"{raw_pdf_path.stem}.txt"
        processed_text_path.parent.mkdir(parents=True, exist_ok=True)
        processed_text_path.write_text(cleaned_text, encoding="utf-8")
        LOGGER.info("Saved cleaned textbook text: %s", processed_text_path)

        return replace(
            record,
            processed_text_path=self._relative(processed_text_path),
            extraction_status="processed",
            review_status="complete",
            notes=f"Processed successfully ({extraction.page_count} pages).",
        )

    def _chunk_single_processed_text(
        self,
        record: TrackerRecord,
        processed_text_path: Path,
        *,
        config: ChunkingConfig,
        overwrite: bool,
    ) -> TrackerRecord:
        try:
            text = processed_text_path.read_text(encoding="utf-8")
        except Exception as exc:
            return replace(
                record,
                chunking_status="failed_chunking",
                notes=self._merge_notes(record.notes, f"Chunking failed: {exc}"),
            )

        chunk_records = build_chunk_records(
            processed_text_path,
            text,
            source_root=self.data_dir,
            config=config,
            content_type=record.content_type or "textbook",
        )

        if not chunk_records:
            return replace(
                record,
                chunking_status="failed_chunking",
                chunk_count="0",
                notes=self._merge_notes(record.notes, "Chunking produced no chunks."),
            )

        subject = record.detected_subject or processed_text_path.parent.name.lower()
        chunk_output_path = self.chunk_dir / subject / f"{processed_text_path.stem}.jsonl"
        try:
            write_chunk_records(chunk_records, chunk_output_path, overwrite=overwrite)
        except FileExistsError:
            return replace(
                record,
                chunking_status="skipped_existing",
                chunk_output_path=self._relative(chunk_output_path),
                chunk_count=str(len(chunk_records)),
                notes=self._merge_notes(record.notes, "Chunk output already exists and overwrite=False."),
            )

        LOGGER.info("Saved chunk file: %s", chunk_output_path)
        return replace(
            record,
            chunk_output_path=self._relative(chunk_output_path),
            chunking_status="chunked",
            chunk_count=str(len(chunk_records)),
            review_status=record.review_status or "complete",
            notes=self._merge_notes(record.notes, f"Chunked into {len(chunk_records)} segments."),
        )

    def _build_base_record(self, pdf_path: Path) -> TrackerRecord:
        return TrackerRecord(
            file_name=pdf_path.name,
            original_path=self._relative(pdf_path),
            extraction_status="discovered",
        )

    def _record_for_processed_file(self, processed_text_path: Path) -> TrackerRecord:
        subject = processed_text_path.parent.name.lower()
        source_name = f"{processed_text_path.stem}.pdf"

        tracker_record = self.tracker.get(source_name, source_name)
        if tracker_record:
            return tracker_record

        relative_processed = self._relative(processed_text_path)
        raw_pdf_path = self.raw_dir / subject / f"{processed_text_path.stem}.pdf"
        return TrackerRecord(
            file_name=source_name,
            original_path=source_name,
            raw_pdf_path=self._relative(raw_pdf_path),
            processed_text_path=relative_processed,
            detected_subject=subject,
            content_type="textbook",
            extraction_status="processed",
            review_status="complete",
            notes="Created tracker record during chunking from processed text.",
        )

    def _record_for_chunk_file(self, chunk_file_path: Path) -> TrackerRecord:
        subject = chunk_file_path.parent.name.lower()
        source_name = f"{chunk_file_path.stem}.pdf"

        tracker_record = self.tracker.get(source_name, source_name)
        if tracker_record:
            if not tracker_record.chunk_output_path:
                return replace(tracker_record, chunk_output_path=self._relative(chunk_file_path))
            return tracker_record

        processed_text_path = self.processed_dir / subject / f"{chunk_file_path.stem}.txt"
        raw_pdf_path = self.raw_dir / subject / f"{chunk_file_path.stem}.pdf"
        return TrackerRecord(
            file_name=source_name,
            original_path=source_name,
            raw_pdf_path=self._relative(raw_pdf_path),
            processed_text_path=self._relative(processed_text_path),
            chunk_output_path=self._relative(chunk_file_path),
            detected_subject=subject,
            content_type="textbook",
            extraction_status="processed",
            chunking_status="chunked",
            review_status="complete",
            notes="Created tracker record during indexing from chunk file.",
        )

    def _copy_to_failed(self, file_path: Path, bucket: str) -> Path:
        destination_dir = self.failed_dir / bucket
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_path = destination_dir / file_path.name
        if not destination_path.exists():
            shutil.copy2(file_path, destination_path)
            LOGGER.warning("Copied problematic PDF into failed bucket: %s", destination_path)
        return destination_path

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root))
        except ValueError:
            return str(path.resolve())

    def _merge_notes(self, existing_notes: str, new_note: str) -> str:
        if not existing_notes:
            return new_note
        if new_note in existing_notes:
            return existing_notes
        return f"{existing_notes} {new_note}"
