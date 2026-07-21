"""Teacher Copilot — Quiz + marking guide generation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.teacher import TEACHER_SYSTEM_PROMPT, build_quiz_prompt
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.teacher import QuizQuestion, QuizRequest, QuizResponse
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 12  # quizzes benefit from more contexts than a single answer


class TeacherQuizService:
    """Produce a quiz with marking guide for a (subject, class, topic)."""

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
    ) -> "TeacherQuizService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def generate(
        self,
        request: QuizRequest,
        *,
        user_id: uuid.UUID | None = None,
    ) -> QuizResponse:
        # Bias retrieval toward exercises + worked examples + definitions —
        # the canonical sources for assessment items.
        results = await asyncio.to_thread(
            self._retriever.retrieve,
            request.topic,
            top_k=_RETRIEVAL_TOP_K,
            subject=request.subject,
            role="teacher",
        )
        logger.info(
            "Teacher quiz | subject=%s class=%s topic=%s n=%s | %s context(s)",
            request.subject,
            request.student_class,
            request.topic[:80],
            request.num_questions,
            len(results),
        )

        user_prompt = build_quiz_prompt(
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            num_questions=request.num_questions,
            difficulty_mix=request.difficulty_mix,
            question_types=request.question_types,
            retrieval_results=results,
        )

        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=TEACHER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_format={"type": "json_object"},
                max_completion_tokens=2500,  # quizzes are long
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError("Quiz generation failed at the LLM step.") from exc

        payload: dict[str, Any] = parse_json_response(response.text)

        questions: list[QuizQuestion] = []
        for raw in payload.get("questions") or []:
            if not isinstance(raw, dict):
                continue
            opts = raw.get("options")
            options: list[str] | None = None
            if isinstance(opts, list) and opts:
                options = [str(o).strip() for o in opts if str(o).strip()]
            try:
                question = QuizQuestion(
                    question_number=int(raw.get("question_number") or (len(questions) + 1)),
                    question=str(raw.get("question", "")).strip(),
                    question_type=str(raw.get("question_type", "short_answer")).strip().lower(),
                    difficulty=str(raw.get("difficulty", "medium")).strip().lower(),  # type: ignore[arg-type]
                    options=options,
                    marking_guide=str(raw.get("marking_guide", "")).strip(),
                    points=int(raw.get("points") or 1),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping malformed quiz question: %s", exc)
                continue
            if question.question:
                questions.append(question)

        total = int(payload.get("total_points") or sum(q.points for q in questions))
        duration = payload.get("suggested_duration_minutes")
        duration_int = int(duration) if isinstance(duration, (int, float)) else None
        sources = sorted({r.source_file for r in results if r.source_file})

        conversation_id = request.conversation_id or uuid.uuid4()

        response_obj = QuizResponse(
            topic=request.topic,
            subject=request.subject,
            student_class=request.student_class,
            questions=questions,
            total_points=total,
            suggested_duration_minutes=duration_int,
            sources=sources,
            conversation_id=conversation_id,
        )

        generation_id = await persist_generation(
            user_id=user_id,
            conversation_id=conversation_id,
            generation_type="quiz",
            title=f"Quiz: {request.topic} ({len(questions)} q)",
            subject=request.subject,
            student_class=request.student_class,
            topic=request.topic,
            request_payload=request,
            response_payload=response_obj,
            sources=sources,
        )
        response_obj.generation_id = generation_id
        return response_obj
