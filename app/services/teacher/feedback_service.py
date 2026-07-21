"""Teacher Copilot — Student feedback generation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.teacher import TEACHER_SYSTEM_PROMPT, build_feedback_prompt
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.teacher import FeedbackRequest, FeedbackResponse
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 6


class TeacherFeedbackService:
    """Generate feedback on a student submission, grounded in the corpus."""

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
    ) -> "TeacherFeedbackService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def generate(
        self,
        request: FeedbackRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> FeedbackResponse:
        # Retrieval blends the question text and the student's answer so the
        # corpus search lands on relevant material regardless of which side
        # carries the topic keywords.
        retrieval_query = (
            request.question.strip() + " " + request.student_answer.strip()
        )[:600]

        results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="teacher",
        )
        logger.info(
            "Teacher feedback | subject=%s class=%s answer_len=%s | %s context(s)",
            request.subject,
            request.student_class,
            len(request.student_answer),
            len(results),
        )

        user_prompt = build_feedback_prompt(
            subject=request.subject,
            student_class=request.student_class,
            question=request.question,
            student_answer=request.student_answer,
            rubric=request.rubric,
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
            raise UpstreamError("Feedback generation failed at the LLM step.") from exc

        payload: dict[str, Any] = parse_json_response(response.text)

        band_raw = str(payload.get("overall_score_band", "fair")).strip().lower()
        if band_raw not in {"excellent", "good", "fair", "needs_improvement"}:
            band_raw = "fair"

        def _string_list(key: str) -> list[str]:
            return [
                str(x).strip()
                for x in (payload.get(key) or [])
                if isinstance(x, str) and x.strip()
            ]

        conversation_id = request.conversation_id or uuid.uuid4()
        sources = sorted({r.source_file for r in results if r.source_file})

        response_obj = FeedbackResponse(
            overall_score_band=band_raw,  # type: ignore[arg-type]
            summary=str(payload.get("summary", "")).strip(),
            strengths=_string_list("strengths"),
            areas_for_improvement=_string_list("areas_for_improvement"),
            specific_corrections=_string_list("specific_corrections"),
            next_steps=_string_list("next_steps"),
            conversation_id=conversation_id,
        )

        # Use a short preview of the question as the title for UI listing.
        title = (
            "Feedback: "
            + (request.question.strip()[:60] + ("…" if len(request.question.strip()) > 60 else ""))
        )
        generation_id = await persist_generation(
            user_id=user_id,
            conversation_id=conversation_id,
            generation_type="student_feedback",
            title=title,
            subject=request.subject,
            student_class=request.student_class,
            topic=None,
            request_payload=request,
            response_payload=response_obj,
            sources=sources,
        )
        response_obj.generation_id = generation_id
        return response_obj
