from app.prompts.student import STUDENT_SYSTEM_PROMPT
from app.rag.query_engine.engine import GroundedQAEngine
from app.rag.retrieval.retriever import RetrievalResult
from app.schemas.student import ConversationTurn, LearningState


class FakeRetriever:
    def __init__(self, retrieval_results):
        self.retrieval_results = retrieval_results
        self.last_query_text = None

    def retrieve(self, query_text, *, top_k=5, subject=None, namespace=None):
        self.last_query_text = query_text
        assert subject == "biology"
        assert top_k == 3
        return self.retrieval_results


class FakeLLMClient:
    def __init__(self):
        self.system_prompt = None
        self.user_prompt = None

    def generate(self, *, system_prompt, user_prompt):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return type("FakeResponse", (), {"text": "Plants use sunlight to make food in a simple way."})


def test_answer_question_passes_student_class_into_prompt():
    retrieval_results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=1,
            text="Plants change sunlight into food inside their leaves.",
        )
    ]

    retriever = FakeRetriever(retrieval_results)
    llm_client = FakeLLMClient()
    engine = GroundedQAEngine(retriever=retriever, llm_client=llm_client)

    result = engine.answer_question(
        "What is photosynthesis?",
        top_k=3,
        subject="biology",
        student_class="Primary 5",
    )

    assert result.answer == "Plants use sunlight to make food in a simple way."
    assert llm_client.system_prompt == STUDENT_SYSTEM_PROMPT
    assert "Primary 5" in llm_client.user_prompt
    assert "Use very simple words" in llm_client.user_prompt
    assert "Hidden supporting material:" in llm_client.user_prompt
    assert "subject:" not in llm_client.user_prompt
    assert "source_file:" not in llm_client.user_prompt
    assert "chunk_index:" not in llm_client.user_prompt


def test_answer_question_uses_history_for_follow_up_retrieval_and_prompt_continuity():
    retrieval_results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=1,
            text="Plants change sunlight into food inside their leaves.",
        )
    ]

    retriever = FakeRetriever(retrieval_results)
    llm_client = FakeLLMClient()
    engine = GroundedQAEngine(retriever=retriever, llm_client=llm_client)
    history = [
        ConversationTurn(role="user", content="What is photosynthesis?"),
        ConversationTurn(role="assistant", content="It is how plants make food."),
    ]

    result = engine.answer_question(
        "Explain it more simply.",
        top_k=3,
        subject="biology",
        student_class="Primary 5",
        history=history,
    )

    assert result.answer == "Plants use sunlight to make food in a simple way."
    assert "Previous student questions: What is photosynthesis?" in retriever.last_query_text
    assert "Current student question: Explain it more simply." in retriever.last_query_text
    assert "Student: What is photosynthesis?" in llm_client.user_prompt
    assert "Tutor: It is how plants make food." in llm_client.user_prompt


def test_answer_question_accepts_dict_history_when_called_outside_fastapi():
    retrieval_results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=1,
            text="Plants change sunlight into food inside their leaves.",
        )
    ]

    retriever = FakeRetriever(retrieval_results)
    llm_client = FakeLLMClient()
    engine = GroundedQAEngine(retriever=retriever, llm_client=llm_client)

    result = engine.answer_question(
        "Explain it more simply.",
        top_k=3,
        subject="biology",
        student_class="Primary 5",
        history=[
            {"role": "user", "content": "What is photosynthesis?"},
            {"role": "assistant", "content": "It is how plants make food."},
        ],
    )

    assert result.answer == "Plants use sunlight to make food in a simple way."
    assert "Previous student questions: What is photosynthesis?" in retriever.last_query_text


def test_answer_question_passes_learning_state_into_prompt():
    retrieval_results = [
        RetrievalResult(
            score=0.9,
            chunk_id="c1",
            document_id="doc1",
            source_file="biology.txt",
            subject="biology",
            content_type="text",
            chunk_index=1,
            text="Plants change sunlight into food inside their leaves.",
        )
    ]

    retriever = FakeRetriever(retrieval_results)
    llm_client = FakeLLMClient()
    engine = GroundedQAEngine(retriever=retriever, llm_client=llm_client)

    result = engine.answer_question(
        "What is photosynthesis?",
        top_k=3,
        subject="biology",
        student_class="SS2",
        learning_state=LearningState(
            understanding_level="high",
            previous_attempt_correct=True,
            attempt_count=1,
        ),
    )

    assert result.answer == "Plants use sunlight to make food in a simple way."
    assert "more technically accurate explanation" in llm_client.user_prompt
    assert "challenging check question" in llm_client.user_prompt
