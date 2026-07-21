"""Dataset discovery, subject classification, and content-type routing.

The corpus is organized on disk as::

    data/
      raw/<content_type>/<subject>/<file>.pdf
      processed/<content_type>/<subject>/<file>.txt
      chunks/<content_type>/<subject>/<file>.jsonl

``content_type`` corresponds to the source folder in the RedEd tree:

    RedEd/Textbooks/<Subject>/*.pdf       → content_type=textbook
    RedEd/Teachers Guide/*.pdf            → content_type=teacher_guide
    RedEd/WAEC Sylabus/<filename>.pdf     → content_type=syllabus

Subject classification:

* Textbooks already live in subject folders → subject = folder name (lowered).
* Teacher guides have no folder structure → keyword-based filename match,
  fallback to ``general`` so nothing is dropped.
* Syllabi have one file per subject, filename IS the subject → keyword match
  on the filename, fallback to ``general``.

Visibility (``student_ok`` / ``teacher_only``) is determined at chunk time
via ``classifier.visibility_for_content_type`` — NOT here.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# --- Subject taxonomy ------------------------------------------------------

# All 18 secondary-school subjects we expect to see in this corpus. The order
# here matches the order folders are searched, so more-specific keywords
# (e.g., "further mathematics") must come BEFORE shorter substrings ("maths").
SUBJECTS: tuple[str, ...] = (
    "further_mathematics",
    "mathematics",
    "english_language",
    "literature_in_english",
    "physics",
    "chemistry",
    "biology",
    "economics",
    "government",
    "civic_education",
    "commerce",
    "accounting",
    "office_practice",
    "computer",
    "history",
    "religious_studies",
    "hausa",
    "igbo",
    "yoruba",
    "general",  # fallback bucket
)

# Keyword → canonical subject mapping for filenames. Tested as case-insensitive
# substring match. First entry that hits wins.
_SUBJECT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("further mathematics", "further_mathematics"),
    ("further maths",       "further_mathematics"),
    ("mathematics",         "mathematics"),
    ("maths",               "mathematics"),
    ("literature in english", "literature_in_english"),
    ("literature",          "literature_in_english"),
    ("english",             "english_language"),
    ("physics",             "physics"),
    ("chemistry",           "chemistry"),
    ("biology",             "biology"),
    ("economics",           "economics"),
    ("financial accounts",  "accounting"),
    ("accounting",          "accounting"),
    ("government",          "government"),
    ("civic",               "civic_education"),
    ("commerce",            "commerce"),
    ("office practice",     "office_practice"),
    ("information and communication technology", "computer"),
    ("ict",                 "computer"),
    ("computer",            "computer"),
    ("computing",           "computer"),
    ("history",             "history"),
    ("nig history",         "history"),
    ("christian religious", "religious_studies"),
    ("islamic religious",   "religious_studies"),
    ("religious",           "religious_studies"),
    ("hausa",               "hausa"),
    ("igbo",                "igbo"),
    ("yoruba",              "yoruba"),
    ("udhr",                "civic_education"),  # human-rights guide
    ("values",              "civic_education"),
)

# --- Content-type taxonomy -------------------------------------------------

CONTENT_TYPES: tuple[str, ...] = (
    "textbook",
    "teacher_guide",
    "syllabus",
)

# Path-component → content_type. Used when copying from organised source
# folders into ``data/raw/<content_type>/...``.
_SOURCE_FOLDER_CONTENT_TYPE: dict[str, str] = {
    "textbooks":      "textbook",
    "textbook":       "textbook",
    "teachers guide": "teacher_guide",
    "teacher guides": "teacher_guide",
    "teacher_guide":  "teacher_guide",
    "waec sylabus":   "syllabus",
    "syllabi":        "syllabus",
    "syllabus":       "syllabus",
}


# --- Filesystem layout helpers --------------------------------------------


def ensure_dataset_structure(data_dir: Path) -> None:
    """Create the expected dataset and failure directories if missing."""

    for content_type in CONTENT_TYPES:
        (data_dir / "raw" / content_type).mkdir(parents=True, exist_ok=True)
        (data_dir / "processed" / content_type).mkdir(parents=True, exist_ok=True)
        (data_dir / "chunks" / content_type).mkdir(parents=True, exist_ok=True)

    (data_dir / "failed" / "scanned_pdfs").mkdir(parents=True, exist_ok=True)
    (data_dir / "failed" / "corrupt_files").mkdir(parents=True, exist_ok=True)
    (data_dir / "failed" / "low_quality_extraction").mkdir(parents=True, exist_ok=True)


def discover_root_pdfs(repo_root: Path) -> list[Path]:
    """Return PDFs found directly under the repository root.

    Kept for backward compatibility with the v1 pipeline. New ingestion
    (step 5+) uses ``discover_source_pdfs`` over the RedEd tree instead.
    """

    return sorted(
        [
            path
            for path in repo_root.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ],
        key=lambda path: path.name.lower(),
    )


# --- Classification --------------------------------------------------------


def classify_subject_from_name(file_name: str) -> str | None:
    """Classify a PDF subject from its filename. Returns None if no match."""

    lower_name = file_name.lower()
    for keyword, subject in _SUBJECT_KEYWORDS:
        if keyword in lower_name:
            return subject
    return None


def classify_subject_from_path(pdf_path: Path) -> str:
    """Best-effort subject classification using the parent folder + filename.

    Returns ``general`` rather than ``None`` when nothing matches — corpus
    discovery should never drop a file silently.
    """

    folder_name = pdf_path.parent.name.lower()
    folder_subject = _normalize_folder_to_subject(folder_name)
    if folder_subject:
        return folder_subject

    inferred = classify_subject_from_name(pdf_path.name)
    if inferred:
        return inferred

    return "general"


def classify_content_type_from_path(pdf_path: Path) -> str | None:
    """Infer ``content_type`` from any segment in the path."""

    for part in pdf_path.parts:
        key = part.strip().lower()
        if key in _SOURCE_FOLDER_CONTENT_TYPE:
            return _SOURCE_FOLDER_CONTENT_TYPE[key]
    return None


def _normalize_folder_to_subject(folder_name: str) -> str | None:
    """Map a textbook subject folder (e.g. 'English Language') to canonical."""

    canonical = folder_name.replace("-", " ").replace("_", " ").strip().lower()
    candidates = {
        "physics": "physics",
        "chemistry": "chemistry",
        "biology": "biology",
        "mathematics": "mathematics",
        "maths": "mathematics",
        "further mathematics": "further_mathematics",
        "english language": "english_language",
        "english": "english_language",
        "literature in english": "literature_in_english",
        "literature": "literature_in_english",
        "economics": "economics",
        "government": "government",
        "civic education": "civic_education",
        "commerce": "commerce",
        "accounting": "accounting",
        "office practice": "office_practice",
        "computer": "computer",
        "ict": "computer",
        "history": "history",
        "religious studies (crs-irs)": "religious_studies",
        "religious studies": "religious_studies",
        "hausa": "hausa",
        "igbo": "igbo",
        "yoruba": "yoruba",
    }
    return candidates.get(canonical)


# --- Copying ---------------------------------------------------------------


def organize_pdf_copy(pdf_path: Path, destination_dir: Path) -> Path:
    """Copy a PDF into the dataset raw directory without deleting the original."""

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / pdf_path.name

    if destination_path.exists():
        LOGGER.info("Raw PDF already organized: %s", destination_path)
        return destination_path

    shutil.copy2(pdf_path, destination_path)
    LOGGER.info("Copied PDF into dataset: %s -> %s", pdf_path, destination_path)
    return destination_path


__all__ = [
    "CONTENT_TYPES",
    "SUBJECTS",
    "classify_content_type_from_path",
    "classify_subject_from_name",
    "classify_subject_from_path",
    "discover_root_pdfs",
    "ensure_dataset_structure",
    "organize_pdf_copy",
]
