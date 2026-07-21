"""Collaborative study group management and AI facilitation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.llm.client import GroqChatClient
from app.prompts.student import (
    STUDENT_STRUCTURED_SYSTEM_PROMPT,
    build_study_group_prompt,
)
from app.rag.retrieval.retriever import PineconeRetriever
from app.schemas.student import (
    StudyGroupDiscussionResponse,
    StudyGroupListResponse,
    StudyGroupResponse,
)
from app.services.student._llm_json import parse_json_response
from app.services.student._ownership import (
    assert_student_id_matches_caller,
    resolve_student_id_for_user,
)
from app.services.student._repositories import (
    InMemoryStudyGroupRepository,
    StudyGroupRecord,
    StudyGroupRepository,
    _new_id,
)
from app.services.student._sql_repositories import SqlStudyGroupRepository

logger = logging.getLogger(__name__)

_RETRIEVAL_TOP_K = 5


class StudentStudyGroupService:
    """Manage study groups and generate AI facilitation content."""

    def __init__(
        self,
        *,
        repository: StudyGroupRepository,
        retriever: PineconeRetriever,
        llm_client: GroqChatClient,
    ) -> None:
        self._repository = repository
        self._retriever = retriever
        self._llm_client = llm_client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "StudentStudyGroupService":
        repository: StudyGroupRepository
        if settings.database_url:
            repository = SqlStudyGroupRepository()
        else:
            repository = InMemoryStudyGroupRepository()
        return cls(
            repository=repository,
            retriever=PineconeRetriever.from_settings(settings, repo_root=repo_root),
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def create_group(
        self,
        *,
        creator_student_id: str,
        name: str,
        subject: str,
        topic: str,
        student_class: str,
        caller_user_id: uuid.UUID | None = None,
    ) -> StudyGroupResponse:
        # Ownership gate — only allow the caller to create a group on
        # their own behalf. Without this a student could open groups
        # under another student's name.
        if caller_user_id is not None:
            await assert_student_id_matches_caller(
                target_student_id=creator_student_id,
                caller_user_id=caller_user_id,
            )
        record = StudyGroupRecord(
            id=_new_id(),
            name=name,
            subject=subject,
            topic=topic,
            student_class=student_class,
            creator_student_id=creator_student_id,
            member_student_ids=[creator_student_id],
        )
        stored = await self._repository.create(record)
        logger.info("Study group created | id=%s | topic=%s", stored.id, topic[:60])
        return self._to_response(stored)

    async def join_group(
        self,
        *,
        group_id: str,
        student_id: str,
        caller_user_id: uuid.UUID | None = None,
    ) -> StudyGroupResponse:
        # Ownership gate — students may only enroll themselves. Without
        # this, anyone could add (or grief-enroll) another student.
        if caller_user_id is not None:
            await assert_student_id_matches_caller(
                target_student_id=student_id,
                caller_user_id=caller_user_id,
            )
        record = await self._repository.get(group_id)
        if record is None:
            raise NotFoundError(f"Study group '{group_id}' does not exist.")

        if student_id not in record.member_student_ids:
            record.member_student_ids.append(student_id)
            await self._repository.update(record)
        return self._to_response(record)

    async def list_groups(
        self,
        *,
        student_class: str | None = None,
        subject: str | None = None,
    ) -> StudyGroupListResponse:
        records = await self._repository.list(
            student_class=student_class,
            subject=subject,
        )
        records.sort(key=lambda r: r.created_at, reverse=True)
        return StudyGroupListResponse(
            groups=[self._to_response(record) for record in records],
        )

    async def facilitate(
        self,
        *,
        group_id: str,
        focus_question: str,
        caller_user_id: uuid.UUID | None = None,
    ) -> StudyGroupDiscussionResponse:
        record = await self._repository.get(group_id)
        if record is None:
            raise NotFoundError(f"Study group '{group_id}' does not exist.")

        # Ownership gate — facilitation runs an LLM call against the
        # group's context, so we restrict it to members. Returning the
        # same NotFoundError shape as 'no such group' prevents leaking
        # which groups exist.
        if caller_user_id is not None:
            resolved = await resolve_student_id_for_user(caller_user_id)
            if resolved is None or resolved.hex not in record.member_student_ids:
                logger.warning(
                    "Study group %s facilitation denied for caller %s",
                    group_id,
                    caller_user_id,
                )
                raise NotFoundError(f"Study group '{group_id}' does not exist.")

        retrieval_results = await asyncio.to_thread(
            self._retriever.retrieve,
            f"{record.subject} {record.topic} {focus_question}",
            top_k=_RETRIEVAL_TOP_K,
            subject=record.subject,
        )

        user_prompt = build_study_group_prompt(
            student_class=record.student_class,
            subject=record.subject,
            topic=record.topic,
            focus_question=focus_question,
            retrieval_results=retrieval_results,
        )

        llm_response = await asyncio.to_thread(
            self._llm_client.generate,
            system_prompt=STUDENT_STRUCTURED_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        parsed = parse_json_response(llm_response.text)

        discussion_questions = _coerce_string_list(parsed.get("discussion_questions"))
        return StudyGroupDiscussionResponse(
            group_id=record.id,
            focus_question=focus_question,
            opening_prompt=str(parsed.get("opening_prompt", "")).strip()
            or "Let's explore this question together.",
            discussion_questions=discussion_questions[:6] if discussion_questions else [
                "What do we already know about this topic?",
                "What is still unclear?",
                "Can someone share an example?",
                "How could we apply this outside class?",
            ],
            shared_insight=str(parsed.get("shared_insight", "")).strip(),
        )

    @staticmethod
    def _to_response(record: StudyGroupRecord) -> StudyGroupResponse:
        return StudyGroupResponse(
            id=record.id,
            name=record.name,
            subject=record.subject,
            topic=record.topic,
            student_class=record.student_class,
            creator_student_id=record.creator_student_id,
            member_student_ids=list(record.member_student_ids),
            created_at=record.created_at,
        )


def _coerce_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


__all__ = ["StudentStudyGroupService"]
