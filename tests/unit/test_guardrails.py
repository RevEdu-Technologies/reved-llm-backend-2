from app.guardrails.content_filter import contains_forbidden_source_language, sanitize_student_answer
from app.guardrails.hallucination_checker import has_large_verbatim_overlap
from app.guardrails.output_formatter import ensure_teacherly_close, strip_numbered_formatting
from app.rag.retrieval.retriever import RetrievalResult


def test_contains_forbidden_source_language_detects_banned_phrases():
    answer = "This explanation is clear, but according to the text the process happens in leaves."
    assert contains_forbidden_source_language(answer)


def test_sanitize_student_answer_removes_banned_source_phrases():
    answer = "According to the material, plants use light."
    cleaned = sanitize_student_answer(answer)
    assert "according to" not in cleaned.lower()
    assert "plants use light" in cleaned


def test_has_large_verbatim_overlap_detects_direct_copy():
    chunk_text = " ".join(f"word{i}" for i in range(26))
    retrieval_results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="science.txt",
            subject="science",
            content_type="text",
            chunk_index=0,
            text=chunk_text,
        )
    ]
    answer = "This is a teacher explanation " + " ".join(f"word{i}" for i in range(25))
    assert has_large_verbatim_overlap(answer, retrieval_results)


# --- Output formatting guardrail tests ---


def test_strip_numbered_formatting_removes_numbered_prefixes():
    answer = "1. Photosynthesis is how plants make food.\n2. It uses sunlight.\n3. Think of it like a factory."
    cleaned = strip_numbered_formatting(answer)
    assert not cleaned.startswith("1.")
    assert "2." not in cleaned
    assert "3." not in cleaned
    assert "Photosynthesis" in cleaned
    assert "sunlight" in cleaned
    assert "factory" in cleaned


def test_strip_numbered_formatting_removes_section_labels():
    answer = (
        "**Direct Answer:** Photosynthesis is how plants make food.\n\n"
        "**Explanation:** It works by capturing sunlight.\n\n"
        "**Example:** Think of a solar panel.\n\n"
        "**Quick Check:** Why is this important?"
    )
    cleaned = strip_numbered_formatting(answer)
    assert "Direct Answer:" not in cleaned
    assert "Explanation:" not in cleaned
    assert "Example:" not in cleaned
    assert "Quick Check:" not in cleaned
    assert "Photosynthesis" in cleaned
    assert "solar panel" in cleaned


def test_strip_numbered_formatting_preserves_clean_prose():
    answer = (
        "Photosynthesis is the process plants use to make food from sunlight.\n\n"
        "This happens in the leaves where chlorophyll traps light energy.\n\n"
        "Why do you think this matters for animals too?"
    )
    cleaned = strip_numbered_formatting(answer)
    assert cleaned == answer.strip()


def test_ensure_teacherly_close_strips_numbered_output():
    answer = "1. Plants make food.\n2. They use sunlight.\n3. Like a factory."
    result = ensure_teacherly_close(answer)
    assert "1." not in result
    assert "2." not in result
    assert "Plants make food" in result

