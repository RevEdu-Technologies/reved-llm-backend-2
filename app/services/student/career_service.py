"""Career guidance service for students."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Sequence

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.student import (
    STUDENT_STRUCTURED_SYSTEM_PROMPT,
    build_career_guidance_prompt,
)
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.student import CareerGuidanceResponse, CareerSuggestion
from app.services.student._llm_json import parse_json_response
from app.services.teacher._persistence import persist_generation

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 4


class StudentCareerService:
    """Generate career-path suggestions tailored to the learner profile."""

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
    ) -> "StudentCareerService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def suggest_paths(
        self,
        *,
        student_class: str,
        favorite_subjects: Sequence[str],
        strengths: Sequence[str],
        interests: Sequence[str],
        long_term_dream: str | None,
        user_id: uuid.UUID | None = None,
    ) -> CareerGuidanceResponse:
        logger.info(
            "Career guidance | class=%s | subjects=%s",
            student_class,
            ",".join(favorite_subjects),
        )

        retrieval_query = _build_retrieval_query(
            favorite_subjects=favorite_subjects,
            interests=interests,
            long_term_dream=long_term_dream,
        )

        retrieval_results = await asyncio.to_thread(
            self._retriever.retrieve,
            retrieval_query,
            top_k=_RETRIEVAL_TOP_K,
        )

        user_prompt = build_career_guidance_prompt(
            student_class=student_class,
            favorite_subjects=favorite_subjects,
            strengths=strengths,
            interests=interests,
            long_term_dream=long_term_dream,
            retrieval_results=retrieval_results,
        )

        llm_response = await asyncio.to_thread(
            self._llm_client.generate,
            system_prompt=STUDENT_STRUCTURED_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        parsed = parse_json_response(llm_response.text)

        raw_suggestions = parsed.get("suggestions")
        if not isinstance(raw_suggestions, list) or not raw_suggestions:
            raise UpstreamError("The AI did not return any career suggestions.")

        suggestions = [_coerce_suggestion(item) for item in raw_suggestions[:5]]

        response = CareerGuidanceResponse(
            student_class=student_class,
            overview=str(parsed.get("overview", "")).strip()
            or "Here are a few career paths that match what you enjoy and do well.",
            suggestions=suggestions,
            encouragement=str(parsed.get("encouragement", "")).strip(),
        )

        title = "Career guidance"
        if long_term_dream:
            title = f"Career guidance: {long_term_dream[:60]}"
        elif favorite_subjects:
            title = f"Career guidance ({', '.join(favorite_subjects[:3])})"

        await persist_generation(
            user_id=user_id,
            conversation_id=uuid.uuid4(),
            role="student",
            generation_type="career_guidance",
            title=title,
            subject=None,
            student_class=student_class,
            topic=long_term_dream,
            request_payload={
                "student_class": student_class,
                "favorite_subjects": list(favorite_subjects),
                "strengths": list(strengths),
                "interests": list(interests),
                "long_term_dream": long_term_dream,
            },
            response_payload=response,
            sources=sorted({r.source_file for r in retrieval_results if r.source_file}),
        )
        return response


def _build_retrieval_query(
    *,
    favorite_subjects: Sequence[str],
    interests: Sequence[str],
    long_term_dream: str | None,
) -> str:
    parts = list(favorite_subjects) + list(interests)
    if long_term_dream:
        parts.append(long_term_dream)
    return " ".join(parts).strip() or "career guidance"


def _coerce_suggestion(item: object) -> CareerSuggestion:
    if not isinstance(item, dict):
        raise UpstreamError("A career suggestion was malformed.")

    subjects = item.get("recommended_subjects")
    next_steps = item.get("next_steps")
    return CareerSuggestion(
        career=str(item.get("career", "")).strip() or "Career path",
        why_it_fits=str(item.get("why_it_fits", "")).strip()
        or "This direction aligns with the learner's strengths.",
        recommended_subjects=_string_list(subjects),
        next_steps=_string_list(next_steps) or [
            "Talk to a mentor who works in this field.",
            "Pick one skill from this area to practise this month.",
        ],
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


__all__ = ["StudentCareerService"]
