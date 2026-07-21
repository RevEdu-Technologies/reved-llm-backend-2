"""Parent — explain-topic service (RAG-grounded plain-language explainer)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.parent import PARENT_SYSTEM_PROMPT, build_explain_topic_prompt
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.parent import ExplainTopicRequest, ExplainTopicResponse
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 6


# --- Streaming event vocabulary -----------------------------------------
#
# Same shape as ``TeacherLessonPlanService``'s — both endpoints
# produce structured JSON, so the streaming contract is "raw chunks
# while we wait, parsed result at the end".


@dataclass(slots=True)
class ExplainTopicStreamMeta:
    """First event — shell-UI context."""

    topic: str
    subject: str
    student_class: str


@dataclass(slots=True)
class ExplainTopicStreamChunk:
    """Raw LLM delta. Renders as a typing indicator or is ignored."""

    text: str


@dataclass(slots=True)
class ExplainTopicStreamDone:
    """Terminal event with the parsed structured payload."""

    result: ExplainTopicResponse


ExplainTopicStreamEvent = (
    ExplainTopicStreamMeta | ExplainTopicStreamChunk | ExplainTopicStreamDone
)


class ParentExplainService:
    """Translate a textbook topic into plain-language guidance for a parent."""

    def __init__(
        self,
        *,
        retriever: PineconeRetriever,
        llm_client: GroqChatClient,
    ) -> None:
        self._retriever = retriever
        self._llm_client = llm_client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "ParentExplainService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def explain(
        self,
        request: ExplainTopicRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> ExplainTopicResponse:
        retrieval_query = (
            f"{request.topic} {request.child_question}"
            if request.child_question
            else request.topic
        )
        results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="parent",  # student_ok only — parents don't see teacher_only material
        )
        logger.info(
            "Parent explain | subject=%s class=%s topic=%s | %s context(s)",
            request.subject,
            request.student_class,
            request.topic[:80],
            len(results),
        )

        prompt = build_explain_topic_prompt(
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            child_question=request.child_question,
            retrieval_results=results,
        )

        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=PARENT_SYSTEM_PROMPT,
                user_prompt=prompt,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError("Topic explanation failed at the LLM step.") from exc

        payload: dict[str, Any] = parse_json_response(response.text)

        sources = sorted({r.source_file for r in results if r.source_file})

        response = ExplainTopicResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            explanation=str(payload.get("explanation", "")).strip(),
            everyday_analogy=str(payload.get("everyday_analogy", "")).strip(),
            things_to_try_at_home=[
                str(t).strip()
                for t in (payload.get("things_to_try_at_home") or [])
                if isinstance(t, str) and t.strip()
            ],
            sources=sources,
        )

        await persist_generation(
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            role="parent",
            generation_type="explain_topic",
            title=f"Explained: {request.topic}",
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            request_payload=request,
            response_payload=response,
            sources=sources,
        )
        return response

    async def explain_stream(
        self,
        request: ExplainTopicRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> AsyncIterator[ExplainTopicStreamEvent]:
        """Streaming counterpart of :meth:`explain`.

        Same pattern as :meth:`TeacherLessonPlanService.generate_stream`:
        retrieval up-front, LLM stream forwarded chunk-by-chunk, parse +
        build + persist at the end, terminal ``done`` carries the parsed
        ``ExplainTopicResponse`` so the frontend can render it directly.
        """

        retrieval_query = (
            f"{request.topic} {request.child_question}"
            if request.child_question
            else request.topic
        )
        results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="parent",
        )
        logger.info(
            "Parent explain stream | subject=%s class=%s topic=%s | %s context(s)",
            request.subject,
            request.student_class,
            request.topic[:80],
            len(results),
        )

        prompt = build_explain_topic_prompt(
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            child_question=request.child_question,
            retrieval_results=results,
        )

        yield ExplainTopicStreamMeta(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
        )

        accumulated: list[str] = []
        try:
            async for delta in self._llm_client.generate_stream(
                system_prompt=PARENT_SYSTEM_PROMPT,
                user_prompt=prompt,
                response_format={"type": "json_object"},
            ):
                accumulated.append(delta)
                yield ExplainTopicStreamChunk(text=delta)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(
                "Topic explanation failed mid-stream."
            ) from exc

        raw = "".join(accumulated)
        payload: dict[str, Any] = parse_json_response(raw)

        sources = sorted({r.source_file for r in results if r.source_file})

        response_obj = ExplainTopicResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            explanation=str(payload.get("explanation", "")).strip(),
            everyday_analogy=str(payload.get("everyday_analogy", "")).strip(),
            things_to_try_at_home=[
                str(t).strip()
                for t in (payload.get("things_to_try_at_home") or [])
                if isinstance(t, str) and t.strip()
            ],
            sources=sources,
        )

        await persist_generation(
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            role="parent",
            generation_type="explain_topic",
            title=f"Explained: {request.topic}",
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            request_payload=request,
            response_payload=response_obj,
            sources=sources,
        )

        yield ExplainTopicStreamDone(result=response_obj)
