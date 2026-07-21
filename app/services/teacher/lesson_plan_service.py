"""Teacher Copilot — Lesson Notes generation."""

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
from app.prompts.teacher import TEACHER_SYSTEM_PROMPT, build_lesson_notes_prompt
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.teacher import (
    LessonNotesRequest,
    LessonNotesResponse,
    LessonSection,
)
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 10


# --- Streaming event vocabulary -----------------------------------------
#
# Mirrors the shape used by ``StudentTutorService.ask_stream`` in
# spirit (meta / chunk / done) — but ``done`` carries a parsed
# ``LessonNotesResponse`` rather than a string ``final_answer``,
# because the output is structured JSON and the frontend is going
# to render it as a structured document, not free text.


@dataclass(slots=True)
class LessonNotesStreamMeta:
    """First event — context the frontend needs to render the shell UI."""

    topic: str
    subject: str
    student_class: str
    conversation_id: uuid.UUID


@dataclass(slots=True)
class LessonNotesStreamChunk:
    """A raw token delta from the LLM (typically JSON characters).

    Frontends that want a typing indicator can render these in a
    "Generating..." preview pane; frontends that just want the
    structured document can ignore chunks and use ``done.result``.
    """

    text: str


@dataclass(slots=True)
class LessonNotesStreamDone:
    """Terminal event carrying the parsed structured payload."""

    result: LessonNotesResponse


LessonNotesStreamEvent = (
    LessonNotesStreamMeta | LessonNotesStreamChunk | LessonNotesStreamDone
)


class TeacherLessonPlanService:
    """Produce grounded lesson notes for a given (subject, class, topic)."""

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
    ) -> "TeacherLessonPlanService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def generate(
        self,
        request: LessonNotesRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> LessonNotesResponse:
        # Build the retrieval query from topic + objectives so semantic match
        # picks up the right textbook sections + syllabus entries.
        query_terms = [request.topic]
        if request.learning_objectives:
            query_terms.extend(request.learning_objectives[:3])
        retrieval_query = " | ".join(query_terms)

        results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="teacher",  # see teacher_only chunks too
        )
        logger.info(
            "Teacher lesson-notes | subject=%s class=%s topic=%s | %s context(s)",
            request.subject,
            request.student_class,
            request.topic[:80],
            len(results),
        )

        user_prompt = build_lesson_notes_prompt(
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            duration_minutes=request.duration_minutes,
            learning_objectives=request.learning_objectives,
            include_examples=request.include_examples,
            retrieval_results=results,
        )

        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=TEACHER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(
                "Lesson notes generation failed at the LLM step."
            ) from exc

        try:
            payload: dict[str, Any] = parse_json_response(response.text)
        except UpstreamError:
            raise

        sections = [
            LessonSection(
                heading=str(s.get("heading", "")).strip() or "Section",
                body=str(s.get("body", "")).strip(),
                examples=[
                    str(ex).strip()
                    for ex in (s.get("examples") or [])
                    if isinstance(ex, str) and ex.strip()
                ],
            )
            for s in (payload.get("sections") or [])
            if isinstance(s, dict)
        ]

        sources = sorted({r.source_file for r in results if r.source_file})

        # Use the caller-supplied conversation_id, or mint a fresh one so
        # the response always carries an id the frontend can carry forward.
        conversation_id = request.conversation_id or uuid.uuid4()

        response_obj = LessonNotesResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            learning_objectives=[
                str(o).strip()
                for o in (payload.get("learning_objectives") or [])
                if isinstance(o, str) and o.strip()
            ],
            overview=str(payload.get("overview", "")).strip(),
            sections=sections,
            teacher_tips=[
                str(t).strip()
                for t in (payload.get("teacher_tips") or [])
                if isinstance(t, str) and t.strip()
            ],
            misconceptions_to_address=[
                str(m).strip()
                for m in (payload.get("misconceptions_to_address") or [])
                if isinstance(m, str) and m.strip()
            ],
            sources=sources,
            conversation_id=conversation_id,
        )

        generation_id = await persist_generation(
            user_id=user_id,
            conversation_id=conversation_id,
            generation_type="lesson_notes",
            title=f"Lesson notes: {request.topic}",
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            request_payload=request,
            response_payload=response_obj,
            sources=sources,
        )
        response_obj.generation_id = generation_id
        return response_obj

    async def generate_stream(
        self,
        request: LessonNotesRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> AsyncIterator[LessonNotesStreamEvent]:
        """Streaming counterpart of :meth:`generate`.

        Retrieval + prompt building run up-front (cheap relative to the
        LLM call). The LLM stream is forwarded chunk-by-chunk for any
        frontend that wants progress feedback; on completion the
        accumulated JSON is parsed, the structured response is built
        and persisted, and the terminal ``done`` event carries the
        full ``LessonNotesResponse`` so the frontend can render it
        without re-parsing the raw chunks.

        Errors bubble out of this generator and are translated to an
        ``error`` SSE event by the route layer.
        """

        query_terms = [request.topic]
        if request.learning_objectives:
            query_terms.extend(request.learning_objectives[:3])
        retrieval_query = " | ".join(query_terms)

        results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="teacher",
        )
        logger.info(
            "Teacher lesson-notes stream | subject=%s class=%s topic=%s | %s context(s)",
            request.subject,
            request.student_class,
            request.topic[:80],
            len(results),
        )

        user_prompt = build_lesson_notes_prompt(
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            duration_minutes=request.duration_minutes,
            learning_objectives=request.learning_objectives,
            include_examples=request.include_examples,
            retrieval_results=results,
        )

        conversation_id = request.conversation_id or uuid.uuid4()

        yield LessonNotesStreamMeta(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            conversation_id=conversation_id,
        )

        accumulated: list[str] = []
        try:
            async for delta in self._llm_client.generate_stream(
                system_prompt=TEACHER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_format={"type": "json_object"},
            ):
                accumulated.append(delta)
                yield LessonNotesStreamChunk(text=delta)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(
                "Lesson notes generation failed mid-stream."
            ) from exc

        raw = "".join(accumulated)
        payload: dict[str, Any] = parse_json_response(raw)

        sections = [
            LessonSection(
                heading=str(s.get("heading", "")).strip() or "Section",
                body=str(s.get("body", "")).strip(),
                examples=[
                    str(ex).strip()
                    for ex in (s.get("examples") or [])
                    if isinstance(ex, str) and ex.strip()
                ],
            )
            for s in (payload.get("sections") or [])
            if isinstance(s, dict)
        ]

        sources = sorted({r.source_file for r in results if r.source_file})

        response_obj = LessonNotesResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            learning_objectives=[
                str(o).strip()
                for o in (payload.get("learning_objectives") or [])
                if isinstance(o, str) and o.strip()
            ],
            overview=str(payload.get("overview", "")).strip(),
            sections=sections,
            teacher_tips=[
                str(t).strip()
                for t in (payload.get("teacher_tips") or [])
                if isinstance(t, str) and t.strip()
            ],
            misconceptions_to_address=[
                str(m).strip()
                for m in (payload.get("misconceptions_to_address") or [])
                if isinstance(m, str) and m.strip()
            ],
            sources=sources,
            conversation_id=conversation_id,
        )

        # Persist BEFORE yielding done so generation_id rides in the
        # terminal event — frontend doesn't need a follow-up round trip
        # to discover the saved generation's id.
        generation_id = await persist_generation(
            user_id=user_id,
            conversation_id=conversation_id,
            generation_type="lesson_notes",
            title=f"Lesson notes: {request.topic}",
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            request_payload=request,
            response_payload=response_obj,
            sources=sources,
        )
        response_obj.generation_id = generation_id

        yield LessonNotesStreamDone(result=response_obj)
