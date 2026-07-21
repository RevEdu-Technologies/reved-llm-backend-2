"""Grounded answer-generation engine built on top of retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.guardrails import (
    contains_forbidden_source_language,
    ensure_teacherly_close,
    has_large_verbatim_overlap,
    sanitize_student_answer,
)
from app.llm.client import GroqChatClient
from app.prompts.student import STUDENT_SYSTEM_PROMPT, build_student_grounded_prompt
from app.rag.retrieval.retriever import PineconeRetriever, RetrievalResult
from app.schemas.student import ConversationTurn, LearningState


@dataclass(slots=True)
class StreamChunk:
    """One text delta from the streaming LLM."""

    text: str


@dataclass(slots=True)
class StreamDone:
    """Sentinel marking the end of a streamed answer.

    Carries the final guarded form. ``was_modified_by_guard`` is True
    when the streamed text was replaced by a fallback (forbidden source
    language, large verbatim overlap, etc.); the frontend should
    discard what it streamed and render ``final_answer`` instead.
    """

    final_answer: str
    was_modified_by_guard: bool
    sources: list["AnswerSource"]
    retrieved_chunks: list[RetrievalResult]


@dataclass(slots=True)
class AnswerSource:
    """Source reference included alongside a grounded answer."""

    source_file: str
    subject: str
    chunk_id: str
    chunk_index: int


@dataclass(slots=True)
class GroundedAnswerResult:
    """Structured answer-generation result."""

    answer: str
    sources: list[AnswerSource]
    retrieved_chunks: list[RetrievalResult]
    used_subject_filter: str | None = None


class GroundedQAEngine:
    """Retrieve relevant chunks and generate a grounded answer with Groq."""

    def __init__(
        self,
        *,
        retriever: PineconeRetriever,
        llm_client: GroqChatClient,
        insufficient_score_threshold: float = 0.45,
    ) -> None:
        self.retriever = retriever
        self.llm_client = llm_client
        self.insufficient_score_threshold = insufficient_score_threshold

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "GroundedQAEngine":
        """Construct the engine from application settings."""

        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    def answer_question(
        self,
        question: str,
        *,
        top_k: int = 5,
        subject: str | None = None,
        student_class: str = "JSS1",
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        namespace: str | None = None,
    ) -> GroundedAnswerResult:
        """Answer a user question with retrieved textbook grounding."""

        prepared = self._prepare(
            question=question,
            top_k=top_k,
            subject=subject,
            student_class=student_class,
            history=history,
            learning_state=learning_state,
            namespace=namespace,
        )
        if prepared.early_result is not None:
            return prepared.early_result

        llm_response = self.llm_client.generate(
            system_prompt=STUDENT_SYSTEM_PROMPT,
            user_prompt=prepared.user_prompt,  # type: ignore[arg-type]
        )
        final_answer = self._guard_answer(
            llm_response.text, prepared.retrieval_results
        )

        return GroundedAnswerResult(
            answer=final_answer,
            sources=self._build_sources(prepared.retrieval_results),
            retrieved_chunks=prepared.retrieval_results,
            used_subject_filter=subject,
        )

    async def stream_answer_question(
        self,
        question: str,
        *,
        top_k: int = 5,
        subject: str | None = None,
        student_class: str = "JSS1",
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        namespace: str | None = None,
    ) -> AsyncIterator[StreamChunk | StreamDone]:
        """Stream a grounded answer token-by-token.

        Yields :class:`StreamChunk` for each LLM delta and a final
        :class:`StreamDone` carrying the guarded text. When the
        retrieved chunks are insufficient (or the LLM stream produces
        no content), we emit a single ``StreamChunk`` with the fallback
        message and then the ``StreamDone`` sentinel — the consumer
        contract is identical for both happy and degenerate paths.

        Retrieval and guarding run synchronously around the stream
        bridge so the I/O cost (Pinecone, guardrails) is paid once per
        request, not per chunk.
        """

        prepared = self._prepare(
            question=question,
            top_k=top_k,
            subject=subject,
            student_class=student_class,
            history=history,
            learning_state=learning_state,
            namespace=namespace,
        )
        retrieval_results = prepared.retrieval_results
        sources = self._build_sources(retrieval_results)

        if prepared.early_result is not None:
            # Insufficient retrieval — emit the fallback as a single
            # chunk so the SSE consumer's chunk-handling path is the
            # same as the happy case.
            yield StreamChunk(text=prepared.early_result.answer)
            yield StreamDone(
                final_answer=prepared.early_result.answer,
                was_modified_by_guard=False,
                sources=sources,
                retrieved_chunks=retrieval_results,
            )
            return

        accumulated: list[str] = []
        async for delta in self.llm_client.generate_stream(
            system_prompt=STUDENT_SYSTEM_PROMPT,
            user_prompt=prepared.user_prompt,  # type: ignore[arg-type]
        ):
            accumulated.append(delta)
            yield StreamChunk(text=delta)

        raw = "".join(accumulated)
        guarded = self._guard_answer(raw, retrieval_results)
        yield StreamDone(
            final_answer=guarded,
            was_modified_by_guard=(guarded != raw),
            sources=sources,
            retrieved_chunks=retrieval_results,
        )

    @dataclass(slots=True)
    class _Prepared:
        """Internal shape: retrieval results + the prompt, OR an early result."""

        retrieval_results: list[RetrievalResult]
        user_prompt: str | None
        early_result: GroundedAnswerResult | None

    def _prepare(
        self,
        *,
        question: str,
        top_k: int,
        subject: str | None,
        student_class: str,
        history: list[ConversationTurn] | None,
        learning_state: LearningState | None,
        namespace: str | None,
    ) -> "GroundedQAEngine._Prepared":
        """Shared retrieval + prompt build for sync and streaming paths."""

        retrieval_query = self._build_retrieval_query(question, history)
        retrieval_results = self.retriever.retrieve(
            retrieval_query,
            top_k=top_k,
            subject=subject,
            namespace=namespace,
        )

        if self._is_insufficient(retrieval_results):
            answer = "I don't have enough information to explain that confidently yet."
            return GroundedQAEngine._Prepared(
                retrieval_results=retrieval_results,
                user_prompt=None,
                early_result=GroundedAnswerResult(
                    answer=answer,
                    sources=self._build_sources(retrieval_results),
                    retrieved_chunks=retrieval_results,
                    used_subject_filter=subject,
                ),
            )

        user_prompt = build_student_grounded_prompt(
            question=question,
            student_class=student_class,
            history=history,
            learning_state=learning_state,
            retrieval_results=retrieval_results,
        )
        return GroundedQAEngine._Prepared(
            retrieval_results=retrieval_results,
            user_prompt=user_prompt,
            early_result=None,
        )

    def _is_insufficient(self, retrieval_results: list[RetrievalResult]) -> bool:
        if not retrieval_results:
            return True
        return retrieval_results[0].score < self.insufficient_score_threshold

    def _guard_answer(
        self,
        answer: str,
        retrieval_results: list[RetrievalResult],
    ) -> str:
        sanitized = sanitize_student_answer(answer)
        sanitized = ensure_teacherly_close(sanitized)

        if contains_forbidden_source_language(sanitized):
            return (
                "I can explain it simply, but I need to avoid mentioning where the information came from. "
                "Please ask again and I will explain it in a clean teacher-style way."
            )

        if has_large_verbatim_overlap(sanitized, retrieval_results):
            return (
                "Here is a simpler explanation: the idea is supported by the lesson material, "
                "but I should explain it in my own words instead of repeating it directly."
            )

        return sanitized

    @staticmethod
    def _build_retrieval_query(
        question: str,
        history: list[ConversationTurn] | list[dict[str, Any]] | None,
    ) -> str:
        """Build a retrieval query that uses prior user turns for follow-up context."""

        if not history:
            return question

        prior_user_questions = [
            content
            for turn in history
            if (content := GroundedQAEngine._extract_turn_content(turn))
            and GroundedQAEngine._extract_turn_role(turn) == "user"
        ]
        if not prior_user_questions:
            return question

        recent_user_context = prior_user_questions[-3:]
        return "\n".join(
            [
                "Previous student questions: " + " | ".join(recent_user_context),
                "Current student question: " + question.strip(),
            ]
        )

    @staticmethod
    def _extract_turn_role(turn: ConversationTurn | dict[str, Any]) -> str:
        if isinstance(turn, dict):
            return str(turn.get("role", "")).strip()
        return turn.role

    @staticmethod
    def _extract_turn_content(turn: ConversationTurn | dict[str, Any]) -> str:
        if isinstance(turn, dict):
            return str(turn.get("content", "")).strip()
        return turn.content.strip()

    @staticmethod
    def _build_sources(retrieval_results: list[RetrievalResult]) -> list[AnswerSource]:
        seen: set[tuple[str, str, int]] = set()
        sources: list[AnswerSource] = []
        for result in retrieval_results:
            key = (result.chunk_id, result.source_file, result.chunk_index)
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                AnswerSource(
                    source_file=result.source_file,
                    subject=result.subject,
                    chunk_id=result.chunk_id,
                    chunk_index=result.chunk_index,
                )
            )
        return sources
