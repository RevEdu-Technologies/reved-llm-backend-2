"""Student tutor service — bridges the API layer to the grounded QA engine."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

from app.core.config import Settings
from app.db.session import session_scope
from app.guardrails import (
    contains_internal_debug_language,
    output_violates_role,
    validate_query_for_role,
)
from app.models.chat import ChatMessage
from app.rag.query_engine.engine import GroundedQAEngine, StreamChunk, StreamDone
from app.rag.query_engine.router import RoleAwareQueryRouter
from app.schemas.student import ConversationTurn, LearningState
from app.services.student._preflight import PreflightResult, StudentPreflight
from app.services.student._subject_matcher import normalize_subject

logger = logging.getLogger(__name__)

_SAFE_FALLBACK_ANSWER = (
    "I can explain this, but I need a moment to phrase it cleanly. "
    "Please ask your question again and I will walk through it step by step."
)


@dataclass(slots=True)
class TutorAnswer:
    """Frontend-safe answer payload returned by the tutor service."""

    status: Literal["answered", "needs_clarification"]
    answer: str
    student_class: str
    subject: str | None
    original_question: str | None = None
    corrected_question: str | None = None
    original_subject: str | None = None
    clarifying_question: str | None = None
    conversation_id: uuid.UUID | None = None


# --- Streaming event vocabulary -----------------------------------------
#
# Yielded by ``StudentTutorService.ask_stream`` and translated to SSE by
# the route layer. Keeping these as typed dataclasses (not raw dicts)
# lets the route's translator pattern-match on type without remembering
# discriminator strings, and lets tests assert on event shape.


@dataclass(slots=True)
class TutorStreamMeta:
    """First event: surfaces what the non-streaming /ask returns in ``data``.

    The frontend uses this to render initial UI state (conversation_id,
    subject pill, did-you-mean banner, clarifier card) before any answer
    text arrives. ``status="needs_clarification"`` means no chunks will
    follow — the next event is the terminal ``TutorStreamDone``.
    """

    status: Literal["answered", "needs_clarification"]
    student_class: str
    subject: str | None
    conversation_id: uuid.UUID | None
    original_question: str | None
    corrected_question: str | None
    original_subject: str | None
    clarifying_question: str | None


@dataclass(slots=True)
class TutorStreamChunk:
    """A token (or token-batch) of the answer."""

    text: str


@dataclass(slots=True)
class TutorStreamDone:
    """Terminal event.

    ``final_answer`` is non-None when the guard layer replaced the
    streamed text (forbidden source language, large verbatim overlap,
    fallback for short/empty completions). The frontend should discard
    what it streamed and render ``final_answer`` instead. ``None``
    means the streamed text was the answer.
    """

    final_answer: str | None


TutorStreamEvent = TutorStreamMeta | TutorStreamChunk | TutorStreamDone


class StudentTutorService:
    """Service layer for student grounded QA.

    Wraps the RoleAwareQueryRouter and returns only the information
    the frontend should see. Internal fields such as sources, retrieved
    chunks, and retrieval scores are intentionally hidden.
    """

    def __init__(
        self,
        *,
        router: RoleAwareQueryRouter,
        preflight: StudentPreflight | None = None,
        persist_chat: bool = True,
    ) -> None:
        self._router = router
        self._preflight = preflight
        self._persist_chat = persist_chat

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "StudentTutorService":
        """Build the service from application settings."""

        engine = GroundedQAEngine.from_settings(settings, repo_root=repo_root)
        return cls(
            router=RoleAwareQueryRouter(engine=engine),
            preflight=StudentPreflight.from_settings(settings),
            persist_chat=bool(settings.database_url),
        )

    async def ask(
        self,
        *,
        question: str,
        student_class: str,
        subject: str | None = None,
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        user_id: uuid.UUID | None = None,
        student_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> TutorAnswer:
        """Generate a grounded, class-aware answer for a student question.

        The question goes through a three-tier preflight first:
          1. Deterministic subject fuzzy match (typos, short forms).
          2. Cheap LLM correction + clarity check.
          3. Safety-net clarifier if tiers 1 and 2 leave the input unresolved.

        If the preflight asks for clarification, we short-circuit and return
        the clarifier instead of calling the answer model.

        ``conversation_id`` groups this turn with prior /ask calls in the
        same thread. If not supplied, a fresh UUID is generated and returned
        in the response so the frontend can carry it forward.
        """

        # Generate a fresh thread id on the first call. We do this even for
        # clarifier / off-topic short-circuit paths so the frontend has an
        # identifier to use on its follow-up.
        is_new_thread = conversation_id is None
        if conversation_id is None:
            conversation_id = uuid.uuid4()

        # Server-side history rehydration: if the caller supplied a
        # conversation_id but no client-side history, replay the stored
        # turns. This lets thin clients use just a thread id without
        # tracking turns themselves.
        if not history and not is_new_thread:
            rehydrated = await self.conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            if rehydrated:
                history = rehydrated
                logger.info(
                    "Student QA | conv=%s rehydrated %s turn(s) from DB",
                    conversation_id,
                    len(rehydrated),
                )

        logger.info(
            "Student QA | conv=%s | class=%s | subject=%s | history_turns=%s | learning_state=%s | question=%s",
            conversation_id,
            student_class,
            subject or "(none)",
            len(history or []),
            "yes" if learning_state else "no",
            question[:80],
        )

        if self._preflight is not None:
            preflight = await self._preflight.run(
                question=question,
                subject_hint=subject,
                student_class=student_class,
                history=history,
            )
        else:
            t1_subject, _ = normalize_subject(subject)
            preflight = PreflightResult(
                original_question=question,
                corrected_question=question,
                original_subject=subject,
                corrected_subject=t1_subject,
                needs_clarification=False,
                clarifying_question=None,
                question_was_corrected=False,
                subject_was_corrected=False,
                subject_source="exact" if t1_subject else "unresolved",
                is_educational=None,
                off_topic_reason=None,
            )

        # Primary educational-intent gate: trust the LLM verdict when it
        # succeeded. When the LLM call failed (is_educational is None), fall
        # back to the hardcoded role validator, which is history-aware.
        if preflight.is_educational is False:
            return TutorAnswer(
                status="answered",
                answer=(
                    preflight.off_topic_reason
                    or "This assistant only helps with educational topics."
                ),
                student_class=student_class,
                subject=subject,
                conversation_id=conversation_id,
            )

        # If the preflight resolved the input to a valid canonical subject,
        # the question is educational by definition — skip the keyword-based
        # educational check that doesn't know about physics sub-topics like
        # "mechanics", "kinematics", etc. (Without this, a brief subject-tagged
        # follow-up like "Newton's second law" gets refused by the role
        # validator's keyword list.)
        skip_edu_check = (
            preflight.is_educational is True
            or preflight.corrected_subject is not None
        )
        validation = validate_query_for_role(
            question,
            "student",
            history=history,
            skip_educational_check=skip_edu_check,
        )
        if not validation.allowed:
            return TutorAnswer(
                status="answered",
                answer=validation.reason or "That question is outside what I can help with here.",
                student_class=student_class,
                subject=subject,
                conversation_id=conversation_id,
            )

        if preflight.needs_clarification and preflight.clarifying_question:
            logger.info(
                "Student QA clarifier | reason=%s | clarifier=%s",
                "ambiguous",
                preflight.clarifying_question[:120],
            )
            return TutorAnswer(
                status="needs_clarification",
                answer="",
                student_class=student_class,
                subject=preflight.corrected_subject,
                original_question=(
                    preflight.original_question
                    if preflight.question_was_corrected
                    else None
                ),
                corrected_question=(
                    preflight.corrected_question
                    if preflight.question_was_corrected
                    else None
                ),
                original_subject=(
                    preflight.original_subject
                    if preflight.subject_was_corrected
                    else None
                ),
                clarifying_question=preflight.clarifying_question,
                conversation_id=conversation_id,
            )

        effective_question = preflight.corrected_question
        effective_subject = preflight.corrected_subject

        result = await asyncio.to_thread(
            self._router.route,
            role="student",
            question=effective_question,
            student_class=student_class,
            subject=effective_subject,
            history=history,
            learning_state=learning_state,
        )

        public_answer = self._build_public_answer(result.answer)

        if self._persist_chat:
            asyncio.create_task(
                self._persist_turn(
                    user_id=user_id,
                    student_id=student_id,
                    conversation_id=conversation_id,
                    subject=effective_subject,
                    grade_level=student_class,
                    question=effective_question,
                    answer=public_answer,
                    result=result,
                )
            )

        return TutorAnswer(
            status="answered",
            answer=public_answer,
            student_class=student_class,
            subject=effective_subject,
            original_question=(
                preflight.original_question if preflight.question_was_corrected else None
            ),
            corrected_question=(
                preflight.corrected_question if preflight.question_was_corrected else None
            ),
            original_subject=(
                preflight.original_subject if preflight.subject_was_corrected else None
            ),
            clarifying_question=None,
            conversation_id=conversation_id,
        )

    async def ask_stream(
        self,
        *,
        question: str,
        student_class: str,
        subject: str | None = None,
        history: list[ConversationTurn] | None = None,
        learning_state: LearningState | None = None,
        user_id: uuid.UUID | None = None,
        student_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
    ) -> AsyncIterator[TutorStreamEvent]:
        """Streaming counterpart of :meth:`ask`.

        Preflight, guardrails, and clarifier logic all run BEFORE the
        first event is yielded — they're cheap relative to the LLM
        call, and yielding before they finish would let the frontend
        render meta state that's about to be replaced by a clarifier.

        On the happy path we yield:
            1. ``TutorStreamMeta(status="answered", ...)``
            2. ``TutorStreamChunk(text=...)`` repeatedly
            3. ``TutorStreamDone(final_answer=None or replacement)``

        On a clarifier short-circuit we yield exactly two events
        (``meta`` with ``status="needs_clarification"`` then ``done``).

        Persistence runs after the stream completes, in a detached task,
        same pattern as :meth:`ask`. If the client disconnects mid-stream,
        the persistence task is NOT spawned — the accumulated text is
        partial and not worth storing.
        """

        # --- Mirror the conversation-id + history setup from ask() -----
        is_new_thread = conversation_id is None
        if conversation_id is None:
            conversation_id = uuid.uuid4()

        if not history and not is_new_thread:
            rehydrated = await self.conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            if rehydrated:
                history = rehydrated
                logger.info(
                    "Student QA stream | conv=%s rehydrated %s turn(s) from DB",
                    conversation_id,
                    len(rehydrated),
                )

        logger.info(
            "Student QA stream | conv=%s | class=%s | subject=%s | "
            "history_turns=%s | learning_state=%s | question=%s",
            conversation_id,
            student_class,
            subject or "(none)",
            len(history or []),
            "yes" if learning_state else "no",
            question[:80],
        )

        # --- Preflight (same as ask()) --------------------------------
        if self._preflight is not None:
            preflight = await self._preflight.run(
                question=question,
                subject_hint=subject,
                student_class=student_class,
                history=history,
            )
        else:
            t1_subject, _ = normalize_subject(subject)
            preflight = PreflightResult(
                original_question=question,
                corrected_question=question,
                original_subject=subject,
                corrected_subject=t1_subject,
                needs_clarification=False,
                clarifying_question=None,
                question_was_corrected=False,
                subject_was_corrected=False,
                subject_source="exact" if t1_subject else "unresolved",
                is_educational=None,
                off_topic_reason=None,
            )

        # --- Educational-intent + role guards (same as ask()) ---------
        if preflight.is_educational is False:
            refusal = (
                preflight.off_topic_reason
                or "This assistant only helps with educational topics."
            )
            yield TutorStreamMeta(
                status="answered",
                student_class=student_class,
                subject=subject,
                conversation_id=conversation_id,
                original_question=None,
                corrected_question=None,
                original_subject=None,
                clarifying_question=None,
            )
            yield TutorStreamChunk(text=refusal)
            yield TutorStreamDone(final_answer=None)
            return

        skip_edu_check = (
            preflight.is_educational is True
            or preflight.corrected_subject is not None
        )
        validation = validate_query_for_role(
            question,
            "student",
            history=history,
            skip_educational_check=skip_edu_check,
        )
        if not validation.allowed:
            refusal = (
                validation.reason
                or "That question is outside what I can help with here."
            )
            yield TutorStreamMeta(
                status="answered",
                student_class=student_class,
                subject=subject,
                conversation_id=conversation_id,
                original_question=None,
                corrected_question=None,
                original_subject=None,
                clarifying_question=None,
            )
            yield TutorStreamChunk(text=refusal)
            yield TutorStreamDone(final_answer=None)
            return

        # --- Clarifier short-circuit (same as ask()) ------------------
        if preflight.needs_clarification and preflight.clarifying_question:
            logger.info(
                "Student QA stream clarifier | reason=%s | clarifier=%s",
                "ambiguous",
                preflight.clarifying_question[:120],
            )
            yield TutorStreamMeta(
                status="needs_clarification",
                student_class=student_class,
                subject=preflight.corrected_subject,
                conversation_id=conversation_id,
                original_question=(
                    preflight.original_question
                    if preflight.question_was_corrected
                    else None
                ),
                corrected_question=(
                    preflight.corrected_question
                    if preflight.question_was_corrected
                    else None
                ),
                original_subject=(
                    preflight.original_subject
                    if preflight.subject_was_corrected
                    else None
                ),
                clarifying_question=preflight.clarifying_question,
            )
            yield TutorStreamDone(final_answer=None)
            return

        # --- Happy path: meta, then chunks, then done -----------------
        effective_question = preflight.corrected_question
        effective_subject = preflight.corrected_subject

        yield TutorStreamMeta(
            status="answered",
            student_class=student_class,
            subject=effective_subject,
            conversation_id=conversation_id,
            original_question=(
                preflight.original_question
                if preflight.question_was_corrected
                else None
            ),
            corrected_question=(
                preflight.corrected_question
                if preflight.question_was_corrected
                else None
            ),
            original_subject=(
                preflight.original_subject
                if preflight.subject_was_corrected
                else None
            ),
            clarifying_question=None,
        )

        streamed_text_parts: list[str] = []
        final_guarded_answer: str | None = None
        engine_done: StreamDone | None = None

        try:
            async for event in self._router.route_stream(
                role="student",
                question=effective_question,
                student_class=student_class,
                subject=effective_subject,
                history=history,
                learning_state=learning_state,
            ):
                if isinstance(event, StreamChunk):
                    streamed_text_parts.append(event.text)
                    yield TutorStreamChunk(text=event.text)
                elif isinstance(event, StreamDone):
                    engine_done = event
                    # Apply the final tutor-public-answer guards (the
                    # same ones ``_build_public_answer`` runs in
                    # ``ask()``) on top of the engine's already-guarded
                    # text.
                    final_guarded_answer = self._build_public_answer(
                        event.final_answer
                    )
        except (asyncio.CancelledError, GeneratorExit):
            logger.info(
                "Student QA stream cancelled mid-flight (conv=%s, "
                "chunks_so_far=%s) — skipping persistence.",
                conversation_id,
                len(streamed_text_parts),
            )
            raise

        streamed_text = "".join(streamed_text_parts)
        if final_guarded_answer is None:
            # Engine never emitted a done event (defensive — shouldn't
            # happen, but fall back to the streamed text under guards).
            final_guarded_answer = self._build_public_answer(streamed_text)

        # Replacement signal: only surface ``final_answer`` if it
        # differs from what the frontend already rendered.
        replacement = (
            final_guarded_answer if final_guarded_answer != streamed_text else None
        )
        yield TutorStreamDone(final_answer=replacement)

        # --- Persist after a complete stream --------------------------
        if self._persist_chat and engine_done is not None:
            asyncio.create_task(
                self._persist_streamed_turn(
                    user_id=user_id,
                    student_id=student_id,
                    conversation_id=conversation_id,
                    subject=effective_subject,
                    grade_level=student_class,
                    question=effective_question,
                    answer=final_guarded_answer,
                    engine_done=engine_done,
                )
            )

    async def _persist_streamed_turn(
        self,
        *,
        user_id: uuid.UUID | None,
        student_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        subject: str | None,
        grade_level: str,
        question: str,
        answer: str,
        engine_done: StreamDone,
    ) -> None:
        """Persist a streamed Q&A turn. Failures are logged but never raised.

        Mirrors :meth:`_persist_turn` but pulls citations/scores from
        the engine's terminal ``StreamDone`` (vs. a
        ``GroundedAnswerResult``). Token counts aren't available on
        streaming chunks (Groq doesn't report them) — stored as None.
        """

        try:
            citations = [
                {
                    "source_file": s.source_file,
                    "subject": s.subject,
                    "chunk_id": s.chunk_id,
                    "chunk_index": s.chunk_index,
                }
                for s in engine_done.sources
            ]
            async with session_scope() as session:
                session.add(
                    ChatMessage(
                        user_id=user_id,
                        student_id=student_id,
                        conversation_id=conversation_id,
                        role="student",
                        subject_hint=subject,
                        grade_level=grade_level,
                        query=question,
                        answer=answer,
                        citations=citations or None,
                        confidence=None,
                        prompt_tokens=None,
                        completion_tokens=None,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chat persistence (stream) failed: %s", exc)

    async def _persist_turn(
        self,
        *,
        user_id: uuid.UUID | None,
        student_id: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        subject: str | None,
        grade_level: str,
        question: str,
        answer: str,
        result: object,
    ) -> None:
        """Persist a tutor Q&A turn. Failures are logged but never raised."""

        try:
            citations = getattr(result, "citations", None)
            confidence = getattr(result, "confidence", None)
            prompt_tokens = getattr(result, "prompt_tokens", None)
            completion_tokens = getattr(result, "completion_tokens", None)
            async with session_scope() as session:
                session.add(
                    ChatMessage(
                        user_id=user_id,
                        student_id=student_id,
                        conversation_id=conversation_id,
                        role="student",
                        subject_hint=subject,
                        grade_level=grade_level,
                        query=question,
                        answer=answer,
                        citations=citations if isinstance(citations, list) else None,
                        confidence=str(confidence) if confidence else None,
                        prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                        completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Chat persistence failed: %s", exc)

    async def list_conversations(
        self,
        *,
        user_id: uuid.UUID | None,
        limit: int = 50,
    ) -> list[dict]:
        """Return the caller's conversation threads, newest first.

        Each entry is a plain dict (not a Pydantic model) so the route can
        map straight to ``ConversationSummary``. Returns an empty list when
        persistence isn't configured or no rows match.
        """

        if not self._persist_chat or user_id is None:
            return []

        from sqlalchemy import select, func

        try:
            async with session_scope() as session:
                stmt = (
                    select(
                        ChatMessage.conversation_id,
                        func.count(ChatMessage.id).label("message_count"),
                        func.min(ChatMessage.created_at).label("started_at"),
                        func.max(ChatMessage.created_at).label("last_active_at"),
                    )
                    .where(
                        ChatMessage.user_id == user_id,
                        ChatMessage.conversation_id.is_not(None),
                    )
                    .group_by(ChatMessage.conversation_id)
                    .order_by(func.max(ChatMessage.created_at).desc())
                    .limit(limit)
                )
                rows = (await session.execute(stmt)).all()
                if not rows:
                    return []

                # Hydrate each thread with subject + latest-question preview.
                summaries: list[dict] = []
                for row in rows:
                    latest = (
                        await session.execute(
                            select(ChatMessage.subject_hint, ChatMessage.query)
                            .where(ChatMessage.conversation_id == row.conversation_id)
                            .order_by(ChatMessage.created_at.desc())
                            .limit(1)
                        )
                    ).first()
                    summaries.append(
                        {
                            "conversation_id": row.conversation_id,
                            "subject": latest.subject_hint if latest else None,
                            "message_count": int(row.message_count),
                            "last_question_preview": (latest.query[:120] if latest else ""),
                            "started_at": row.started_at,
                            "last_active_at": row.last_active_at,
                        }
                    )
                return summaries
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Conversation list failed: %s", exc)
            return []

    async def conversation_history(
        self,
        *,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> list[ConversationTurn]:
        """Return chronologically-ordered turns for ``conversation_id``.

        Access control: only the row's owning ``user_id`` can read; mismatches
        return an empty list (treated identically to "no such conversation"
        so we don't leak thread existence).
        """

        if not self._persist_chat:
            return []

        from sqlalchemy import select

        try:
            async with session_scope() as session:
                stmt = (
                    select(ChatMessage.query, ChatMessage.answer, ChatMessage.user_id)
                    .where(ChatMessage.conversation_id == conversation_id)
                    .order_by(ChatMessage.created_at.asc())
                )
                rows = (await session.execute(stmt)).all()
                if not rows:
                    return []
                # Auth: every row must belong to the caller. We compare the
                # FIRST row's user_id; mixed-owner threads shouldn't exist
                # since conversation_id is generated per-user.
                first_owner = rows[0].user_id
                # Defensive: deny if the caller has no user_id (cannot
                # prove ownership) or if the first row's owner is NULL
                # (orphaned thread) or mismatched. Same empty-list shape
                # as "no such conversation" — never leak thread existence.
                if user_id is None or first_owner is None or first_owner != user_id:
                    logger.warning(
                        "Conversation %s read denied for user %s (owner=%s)",
                        conversation_id, user_id, first_owner,
                    )
                    return []

                turns: list[ConversationTurn] = []
                for row in rows:
                    if row.query:
                        turns.append(ConversationTurn(role="user", content=row.query))
                    if row.answer:
                        turns.append(ConversationTurn(role="assistant", content=row.answer))
                return turns
        except Exception as exc:  # noqa: BLE001
            logger.warning("Conversation history fetch failed: %s", exc)
            return []

    @staticmethod
    def _build_public_answer(answer: str) -> str:
        """Return a learner-safe answer string with no obvious internal leakage."""

        if not answer or not answer.strip():
            return _SAFE_FALLBACK_ANSWER
        if contains_internal_debug_language(answer):
            return _SAFE_FALLBACK_ANSWER
        if output_violates_role(answer, "student"):
            return _SAFE_FALLBACK_ANSWER
        return answer
