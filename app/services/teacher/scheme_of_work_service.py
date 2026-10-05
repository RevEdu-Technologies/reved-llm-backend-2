"""Teacher Copilot — scheme-of-work (weekly topic list) generation.

Backs ``GET /teacher/scheme-of-work``. There is no ingested official
NERDC/WAEC/NECO scheme-of-work document in this system today — this service
grounds the week-by-week breakdown in whatever textbook material the corpus
has for the (subject, class) pair, plus the model's general knowledge of
Nigerian curriculum pacing, and says so in ``SchemeOfWorkResponse.note``.

Results are cached (not re-generated on every call): curriculum pacing is
stable reference content, and the frontend itself caches this for 24h, so a
24h server-side TTL keyed on (subject, student_class, term) is enough to
keep repeat calls cheap without ever serving genuinely stale data for longer
than a school day.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import UpstreamError
from app.llm.client import GroqChatClient
from app.prompts.teacher import TEACHER_SYSTEM_PROMPT, build_scheme_of_work_prompt
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.teacher import SchemeOfWorkResponse, SchemeOfWorkWeek
from app.services.cache import cached_call
from app.services.student._llm_json import parse_json_response
from app.utils.subjects import normalize_subject

logger = logging.getLogger(__name__)

NS_SCHEME_OF_WORK = "scheme-of-work"
_RETRIEVAL_TOP_K = 15
_CACHE_TTL_SECONDS = 86_400.0  # 24h — matches the frontend's own cache window.


class TeacherSchemeOfWorkService:
    """Produce (and cache) a term's weekly topic breakdown."""

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
    ) -> "TeacherSchemeOfWorkService":
        return cls(
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def get_weeks(
        self,
        *,
        subject: str,
        student_class: str,
        term: int,
    ) -> SchemeOfWorkResponse:
        identifier = f"{subject}:{student_class}:{term}"

        async def _loader() -> dict[str, Any]:
            response = await self._compute(
                subject=subject, student_class=student_class, term=term
            )
            return response.model_dump(mode="json")

        data = await cached_call(
            namespace=NS_SCHEME_OF_WORK,
            identifier=identifier,
            ttl_seconds=_CACHE_TTL_SECONDS,
            loader=_loader,
        )
        return SchemeOfWorkResponse.model_validate(data)

    async def _compute(
        self,
        *,
        subject: str,
        student_class: str,
        term: int,
    ) -> SchemeOfWorkResponse:
        canonical_subject, _score = normalize_subject(subject)
        retrieval_subject = (
            canonical_subject if canonical_subject not in (None, "general") else None
        )

        try:
            results = await asyncio.to_thread(
                self._retriever.retrieve,
                f"{subject} {student_class} term {term} syllabus units topics",
                top_k=_RETRIEVAL_TOP_K,
                subject=retrieval_subject,
                role="teacher",
            )
        except Exception as exc:  # noqa: BLE001 - unknown subject/class should 200 with [], not 500
            logger.warning(
                "Scheme-of-work retrieval failed (subject=%s class=%s term=%s): %s",
                subject, student_class, term, exc,
            )
            results = []

        if not results:
            # No corpus coverage for this subject/class. Returning an empty
            # list (rather than 404/500) matches the contract: unknown
            # subject/class should 200 with {"weeks": []}.
            logger.info(
                "Scheme-of-work: no retrieval hits for subject=%s class=%s term=%s; "
                "returning empty weeks.",
                subject, student_class, term,
            )
            return SchemeOfWorkResponse(
                subject=subject, student_class=student_class, term=term, weeks=[]
            )

        user_prompt = build_scheme_of_work_prompt(
            subject=subject,
            student_class=student_class,
            term=term,
            retrieval_results=results,
        )

        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=TEACHER_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                response_format={"type": "json_object"},
                max_completion_tokens=2048,
            )
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(
                "Scheme-of-work generation failed at the LLM step."
            ) from exc

        payload = parse_json_response(response.text)

        weeks = [
            SchemeOfWorkWeek(
                week=int(w.get("week", i + 1)),
                topic=str(w.get("topic", "")).strip() or "Untitled",
                subtopics=[
                    str(s).strip()
                    for s in (w.get("subtopics") or [])
                    if isinstance(s, str) and s.strip()
                ],
                objectives=[
                    str(o).strip()
                    for o in (w.get("objectives") or [])
                    if isinstance(o, str) and o.strip()
                ],
            )
            for i, w in enumerate(payload.get("weeks") or [])
            if isinstance(w, dict)
        ]
        weeks.sort(key=lambda w: w.week)

        return SchemeOfWorkResponse(
            subject=subject,
            student_class=student_class,
            term=term,
            weeks=weeks,
        )
