"""Goal setting and tracking for students.

Goals are motivational rather than factual, so coaching notes are generated
with the LLM directly and are intentionally not grounded in the textbook
corpus. The shared system prompt still forbids off-topic content.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.llm.client import GroqChatClient
from app.prompts.student import build_goal_coaching_prompt
from app.schemas.student import GoalListResponse, GoalResponse
from app.services.student._ownership import (
    assert_goal_owned_by_caller,
    assert_student_id_matches_caller,
)
from app.services.student._repositories import (
    GoalRecord,
    GoalRepository,
    InMemoryGoalRepository,
    _new_id,
    _now,
    replace,
)
from app.services.student._sql_repositories import SqlGoalRepository

logger = logging.getLogger(__name__)

_COACH_SYSTEM_PROMPT = (
    "You are a warm, focused student coach. "
    "Write only plain-text encouragement, never markdown or lists. "
    "Keep responses within 2-3 sentences."
)


class StudentGoalService:
    """Create, retrieve, and update student learning goals."""

    def __init__(
        self,
        *,
        repository: GoalRepository,
        llm_client: GroqChatClient | None,
    ) -> None:
        self._repository = repository
        self._llm_client = llm_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "StudentGoalService":
        repository: GoalRepository
        if settings.database_url:
            repository = SqlGoalRepository()
        else:
            repository = InMemoryGoalRepository()
        return cls(
            repository=repository,
            llm_client=GroqChatClient.from_settings(settings),
        )

    async def create_goal(
        self,
        *,
        student_id: str,
        title: str,
        description: str | None,
        subject: str | None,
        target_date: date | None,
        caller_user_id: uuid.UUID | None = None,
    ) -> GoalResponse:
        # Ownership gate — the caller may only create goals against
        # their own Student row. Raises NotFoundError (→ 404) on mismatch
        # so attackers cannot enumerate other students' UUIDs.
        if caller_user_id is not None:
            await assert_student_id_matches_caller(
                target_student_id=student_id,
                caller_user_id=caller_user_id,
            )
        now = _now()
        coaching_note = await self._coaching_note(
            title=title,
            description=description,
            subject=subject,
            progress_percent=0,
            recent_note=None,
        )
        record = GoalRecord(
            id=_new_id(),
            student_id=student_id,
            title=title,
            description=description,
            subject=subject,
            target_date=target_date,
            progress_percent=0,
            status="active",
            coaching_note=coaching_note,
            created_at=now,
            updated_at=now,
        )
        stored = await self._repository.create(record)
        logger.info("Goal created | student=%s | goal=%s", student_id, stored.id)
        return self._to_response(stored)

    async def list_goals(
        self,
        *,
        student_id: str,
        caller_user_id: uuid.UUID | None = None,
    ) -> GoalListResponse:
        # Ownership gate — listing another student's goals would leak
        # private learning data.
        if caller_user_id is not None:
            await assert_student_id_matches_caller(
                target_student_id=student_id,
                caller_user_id=caller_user_id,
            )
        records = await self._repository.list_for_student(student_id)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return GoalListResponse(
            student_id=student_id,
            goals=[self._to_response(record) for record in records],
        )

    async def update_progress(
        self,
        *,
        goal_id: str,
        progress_percent: int,
        note: str | None,
        caller_user_id: uuid.UUID | None = None,
    ) -> GoalResponse:
        # Ownership gate — fetch the goal and confirm the caller's
        # Student row matches before mutating progress. Raises 404 on
        # mismatch so attackers cannot probe for valid goal UUIDs.
        if caller_user_id is not None:
            await assert_goal_owned_by_caller(
                goal_id=goal_id, caller_user_id=caller_user_id
            )
        record = await self._repository.get(goal_id)
        if record is None:
            raise NotFoundError(f"Goal '{goal_id}' does not exist.")

        new_status = "completed" if progress_percent >= 100 else "active"
        coaching_note = await self._coaching_note(
            title=record.title,
            description=record.description,
            subject=record.subject,
            progress_percent=progress_percent,
            recent_note=note,
        )
        updated = replace(
            record,
            progress_percent=progress_percent,
            status=new_status,
            coaching_note=coaching_note,
            updated_at=_now(),
        )
        stored = await self._repository.update(updated)
        logger.info(
            "Goal progress | student=%s | goal=%s | progress=%d | status=%s",
            stored.student_id,
            stored.id,
            stored.progress_percent,
            stored.status,
        )
        if new_status == "completed":
            await self._emit_goal_achieved(stored)
        return self._to_response(stored)

    @staticmethod
    async def _emit_goal_achieved(stored: GoalRecord) -> None:
        """Best-effort goal.achieved webhook when a goal hits 100%.

        Emitted in its own transaction (the goal write went through the
        repository abstraction, which doesn't hand back a session to enlist).
        ``school_id`` isn't resolved here — only global subscribers receive
        it; the payload carries ``student_id`` so a subscriber can map it.
        """
        try:
            from app.core.webhooks import EVENT_GOAL_ACHIEVED
            from app.services.webhook_service import WebhookService

            await WebhookService().emit(
                event_type=EVENT_GOAL_ACHIEVED,
                data={
                    "goal_id": str(stored.id),
                    "student_id": str(stored.student_id),
                    "title": stored.title,
                    "subject": stored.subject,
                    "progress_percent": stored.progress_percent,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("webhook emit (goal.achieved) failed: %s", exc)

    async def _coaching_note(
        self,
        *,
        title: str,
        description: str | None,
        subject: str | None,
        progress_percent: int,
        recent_note: str | None,
    ) -> str:
        if self._llm_client is None:
            return _fallback_coaching_note(progress_percent)

        prompt = build_goal_coaching_prompt(
            title=title,
            description=description,
            subject=subject,
            progress_percent=progress_percent,
            recent_note=recent_note,
        )
        try:
            response = await asyncio.to_thread(
                self._llm_client.generate,
                system_prompt=_COACH_SYSTEM_PROMPT,
                user_prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001 - coaching is non-critical
            logger.warning("Goal coaching fallback (LLM error): %s", exc)
            return _fallback_coaching_note(progress_percent)

        text = response.text.strip()
        return text or _fallback_coaching_note(progress_percent)

    @staticmethod
    def _to_response(record: GoalRecord) -> GoalResponse:
        return GoalResponse(
            id=record.id,
            student_id=record.student_id,
            title=record.title,
            description=record.description,
            subject=record.subject,
            target_date=record.target_date,
            progress_percent=record.progress_percent,
            status=record.status,
            coaching_note=record.coaching_note,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def _fallback_coaching_note(progress_percent: int) -> str:
    if progress_percent >= 100:
        return "Well done on completing this goal. Take a moment to celebrate, then pick your next challenge."
    if progress_percent >= 50:
        return "You're more than halfway there — keep the momentum going with one focused study session today."
    if progress_percent > 0:
        return "A great start. Break the next part into a small step you can finish this week."
    return "Every goal starts with a first step. Choose one small action you can do today to begin."


__all__ = ["StudentGoalService"]
