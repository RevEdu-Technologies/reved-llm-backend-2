"""Personalized learning pathway generation for students."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uuid

from app.core.config import Settings
from app.core.errors import UpstreamError, ValidationError
from app.llm.client import GroqChatClient
from app.prompts.student import (
    STUDENT_STRUCTURED_SYSTEM_PROMPT,
    build_learning_path_prompt,
)
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.student import LearningPathResponse, LearningPathStep
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_MIN_STEPS = 3
_MAX_STEPS = 8
_RETRIEVAL_TOP_K = 6


class StudentLearningPathService:
    """Generate a personalized study pathway for a given topic."""

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
    ) -> "StudentLearningPathService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def generate_path(
        self,
        *,
        student_class: str,
        subject: str,
        topic: str,
        current_understanding: str | None,
        weekly_study_hours: int | None,
        user_id: uuid.UUID | None = None,
    ) -> LearningPathResponse:
        """Generate a learning pathway grounded in the textbook corpus."""

        logger.info(
            "Learning path | class=%s | subject=%s | topic=%s",
            student_class,
            subject,
            topic[:80],
        )

        retrieval_results = await asyncio.to_thread(
            self._retriever.retrieve,
            f"{subject} {topic}",
            top_k=_RETRIEVAL_TOP_K,
            subject=subject,
        )

        user_prompt = build_learning_path_prompt(
            student_class=student_class,
            subject=subject,
            topic=topic,
            understanding=current_understanding,
            weekly_hours=weekly_study_hours,
            retrieval_results=retrieval_results,
        )

        llm_response = await asyncio.to_thread(
            self._llm_client.generate,
            system_prompt=STUDENT_STRUCTURED_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        parsed = parse_json_response(llm_response.text)
        steps = self._coerce_steps(parsed.get("steps"))

        response = LearningPathResponse(
            topic=topic,
            subject=subject,
            student_class=student_class,
            overview=str(parsed.get("overview", "")).strip()
            or "Here is a personalized plan to master this topic step by step.",
            steps=steps,
            encouragement=str(parsed.get("encouragement", "")).strip(),
        )

        await persist_generation(
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            role="student",
            generation_type="learning_path",
            title=f"Learning path: {topic}",
            subject=subject,
            student_class=student_class,
            topic=topic,
            request_payload={
                "student_class": student_class,
                "subject": subject,
                "topic": topic,
                "current_understanding": current_understanding,
                "weekly_study_hours": weekly_study_hours,
            },
            response_payload=response,
            sources=sorted({r.source_file for r in retrieval_results if r.source_file}),
        )
        return response

    @staticmethod
    def _coerce_steps(raw_steps: object) -> list[LearningPathStep]:
        if not isinstance(raw_steps, list) or not raw_steps:
            raise UpstreamError("The learning pathway had no steps.")

        if len(raw_steps) < _MIN_STEPS:
            raise UpstreamError("The learning pathway did not have enough steps.")
        if len(raw_steps) > _MAX_STEPS:
            raw_steps = raw_steps[:_MAX_STEPS]

        steps: list[LearningPathStep] = []
        for index, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValidationError("Each learning-pathway step must be an object.")
            try:
                step = LearningPathStep(
                    order=int(item.get("order", index)),
                    title=str(item.get("title", f"Step {index}")).strip() or f"Step {index}",
                    focus=str(item.get("focus", "")).strip() or "Focus area",
                    suggested_activity=str(item.get("suggested_activity", "")).strip()
                    or "Study and practice this focus area.",
                    estimated_hours=float(item.get("estimated_hours", 1.0)),
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"Invalid learning-pathway step at index {index}.") from exc
            steps.append(step)
        return steps


__all__ = ["StudentLearningPathService"]
