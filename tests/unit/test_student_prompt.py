from app.prompts.base import load_template
from app.prompts.student import STUDENT_SYSTEM_PROMPT, build_student_grounded_prompt
from app.rag.retrieval.retriever import RetrievalResult
from app.schemas.student import ConversationTurn, LearningState


def test_build_student_grounded_prompt_hides_metadata_and_supports_student_class():
    retrieval_results = [
        RetrievalResult(
            score=0.87,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=2,
            text="Photosynthesis is the process plants use to turn sunlight into food.",
        )
    ]

    prompt = build_student_grounded_prompt(
        question="What is photosynthesis?",
        student_class="JSS1",
        history=None,
        learning_state=None,
        retrieval_results=retrieval_results,
    )

    assert "source_file" not in prompt
    assert "chunk_index" not in prompt
    assert "document_id" not in prompt
    assert "context says" not in prompt.lower()
    assert "Hidden supporting material:" in prompt
    assert "JSS1" in prompt
    assert "Use simple but correct school-level language" in prompt


def test_build_student_grounded_prompt_primary_guidance_is_simple():
    retrieval_results = [
        RetrievalResult(
            score=0.95,
            chunk_id="c2",
            document_id="doc2",
            source_file="science.txt",
            subject="science",
            content_type="text",
            chunk_index=0,
            text="A plant uses light to make food in its leaves.",
        )
    ]

    prompt = build_student_grounded_prompt(
        question="How do plants make food?",
        student_class="Primary 4",
        history=None,
        learning_state=None,
        retrieval_results=retrieval_results,
    )

    assert "very simple words" in prompt
    assert "Avoid heavy scientific terms" in prompt


def test_build_student_grounded_prompt_includes_history_for_continuity():
    retrieval_results = [
        RetrievalResult(
            score=0.95,
            chunk_id="c2",
            document_id="doc2",
            source_file="science.txt",
            subject="science",
            content_type="text",
            chunk_index=0,
            text="A plant uses light to make food in its leaves.",
        )
    ]
    history = [
        ConversationTurn(role="user", content="What is photosynthesis?"),
        ConversationTurn(role="assistant", content="It is how plants make food."),
    ]

    prompt = build_student_grounded_prompt(
        question="Explain it more simply.",
        student_class="Primary 4",
        history=history,
        learning_state=None,
        retrieval_results=retrieval_results,
    )

    assert "Previous conversation for continuity only:" in prompt
    assert "Student: What is photosynthesis?" in prompt
    assert "Tutor: It is how plants make food." in prompt
    assert "Do not treat the previous conversation as factual grounding." in prompt


def test_build_student_grounded_prompt_low_understanding_adds_simplify_guidance():
    retrieval_results = [
        RetrievalResult(
            score=0.95,
            chunk_id="c2",
            document_id="doc2",
            source_file="science.txt",
            subject="science",
            content_type="text",
            chunk_index=0,
            text="A plant uses light to make food in its leaves.",
        )
    ]

    prompt = build_student_grounded_prompt(
        question="What is photosynthesis?",
        student_class="Primary 5",
        history=None,
        learning_state=LearningState(
            understanding_level="low",
            previous_attempt_correct=False,
            attempt_count=2,
        ),
        retrieval_results=retrieval_results,
    )

    assert "The student seems to be struggling" in prompt
    assert "break the idea into smaller steps" in prompt
    assert "Slow down further" in prompt


def test_build_student_grounded_prompt_high_understanding_adds_deeper_guidance():
    retrieval_results = [
        RetrievalResult(
            score=0.95,
            chunk_id="c2",
            document_id="doc2",
            source_file="science.txt",
            subject="science",
            content_type="text",
            chunk_index=0,
            text="A plant uses light to make food in its leaves.",
        )
    ]

    prompt = build_student_grounded_prompt(
        question="What is photosynthesis?",
        student_class="SS2",
        history=None,
        learning_state=LearningState(
            understanding_level="high",
            previous_attempt_correct=True,
            attempt_count=1,
        ),
        retrieval_results=retrieval_results,
    )

    assert "more technically accurate explanation" in prompt
    assert "more challenging check question" in prompt
    assert "you may progress the explanation naturally" in prompt


def test_student_qa_template_does_not_instruct_numbered_format():
    template = load_template("student_qa.txt")
    assert "1. Direct answer" not in template
    assert "2. Clear explanation" not in template
    assert "3. Simple example" not in template
    assert "4. Quick learning" not in template


def test_student_qa_template_instructs_natural_prose_and_adaptive_tutoring():
    template = load_template("student_qa.txt")
    assert "natural flowing paragraphs" in template
    assert "not as a numbered list" in template
    assert "Adaptive tutoring guidance:" in template
    assert "Do not mention internal labels such as learning state" in template


def test_student_system_prompt_does_not_instruct_numbered_format():
    assert "1." not in STUDENT_SYSTEM_PROMPT
    assert "2." not in STUDENT_SYSTEM_PROMPT
    assert "Give one direct answer, then" not in STUDENT_SYSTEM_PROMPT


def test_student_system_prompt_instructs_natural_prose_history_limits_and_adaptation():
    assert "natural flowing paragraphs" in STUDENT_SYSTEM_PROMPT
    assert "Do not number the response" in STUDENT_SYSTEM_PROMPT
    assert "Do not treat prior conversation as factual grounding." in STUDENT_SYSTEM_PROMPT
    assert "adapt naturally without naming or exposing those signals" in STUDENT_SYSTEM_PROMPT
