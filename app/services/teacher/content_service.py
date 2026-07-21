"""Teacher Copilot — frontend-compatible markdown content generation.

This service backs ``POST /teacher/generate-content``. Unlike the
structured-JSON lesson-notes / quiz / feedback services, it produces a
single Markdown document streamed token-by-token, matching the payload
and stream shape the RevEd web app already uses. The win over the
frontend's previous (ungrounded) generator is RAG grounding: retrieval
runs with ``role="teacher"`` so the model sees both student-visible
textbooks and teacher-only material.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.teacher import (
    TEACHER_CONTENT_SYSTEM_PROMPT,
    build_content_markdown_prompt,
)
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.teacher import TeacherContentRequest
from app.utils.subjects import normalize_subject

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 10
# Markdown artefacts (lesson plans, study guides) are long-form — the
# default ~700-token answer cap would truncate them. Give the content
# path a generous ceiling.
_CONTENT_MAX_TOKENS = 2048


class TeacherContentService:
    """Stream grounded markdown teaching materials."""

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
    ) -> "TeacherContentService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def generate_stream(
        self,
        request: TeacherContentRequest,
    ) -> AsyncIterator[str]:
        """Yield markdown deltas for the requested artefact.

        Retrieval + prompt building run up front; the LLM stream is then
        forwarded delta-by-delta. Raises :class:`UpstreamError` on a
        provider failure so the route can emit a terminal error frame.
        """

        student_class = request.student_class
        # Use a confidently-matched canonical subject as a retrieval filter;
        # fall back to no filter (None) for umbrella/free-text subjects so we
        # don't over-restrict and return zero contexts.
        canonical_subject, _score = normalize_subject(request.subject)
        retrieval_subject = (
            canonical_subject if canonical_subject not in (None, "general") else None
        )

        results = await asyncio.to_thread(
            self._retriever.retrieve,
            request.topic,
            top_k=_RETRIEVAL_TOP_K,
            subject=retrieval_subject,
            role="teacher",
        )
        logger.info(
            "Teacher content | type=%s subject=%s class=%s topic=%s | %s context(s)",
            request.contentType,
            request.subject,
            student_class,
            request.topic[:80],
            len(results),
        )

        user_prompt = build_content_markdown_prompt(
            content_type=request.contentType,
            subject=request.subject,
            student_class=student_class,
            topic=request.topic,
            learning_objectives=request.learningObjectives,
            difficulty_level=request.difficultyLevel,
            curriculum_standard=request.curriculumStandard,
            tone=request.tone,
            retrieval_results=results,
        )

        try:
            async for delta in self._llm_client.generate_stream(
                system_prompt=TEACHER_CONTENT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                max_completion_tokens=_CONTENT_MAX_TOKENS,
            ):
                yield delta
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(
                "Content generation failed at the LLM step."
            ) from exc
