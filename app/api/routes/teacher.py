"""Teacher Copilot API routes.

All endpoints require ``role=teacher`` (or ``admin``). In dev mode
(``AUTH_ENABLED=false``) you can hit them via Swagger by adding the
``X-Dev-Role: teacher`` request header; with auth enabled, a Supabase JWT
whose ``app_metadata.role`` is ``teacher`` is required.

Every route returns the standard RevEd envelope
``{"status", "data", "message", "role"}`` so the frontend can parse all
responses the same way.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api._pagination import clamp_limit, decode_cursor
from app.api._sse import (
    OPENAI_SSE_DONE,
    SSE_MEDIA_TYPE,
    SSE_RESPONSE_HEADERS,
    format_openai_chunk,
    format_openai_error,
    format_sse,
)
from app.api.dependencies import (
    get_content_service,
    get_feedback_service,
    get_lesson_plan_service,
    get_progress_service,
    get_quiz_service,
)
from app.core.rate_limit import limiter, llm_limit_for_key, tiered_rate_limit_key
from app.core.security import AuthenticatedUser, require_role
from app.services.teacher.lesson_plan_service import (
    LessonNotesStreamChunk,
    LessonNotesStreamDone,
    LessonNotesStreamMeta,
)
from app.schemas.common import APIResponse
from app.schemas.teacher import (
    ClassProgressResponse,
    FeedbackRequest,
    FeedbackResponse,
    LessonNotesRequest,
    LessonNotesResponse,
    QuizRequest,
    QuizResponse,
    TeacherContentRequest,
    TeacherGenerationDetail,
    TeacherGenerationListResponse,
    TeacherGenerationSummary,
)
from app.services.teacher.content_service import TeacherContentService
from app.services.teacher._persistence import (
    get_generation_for_user,
    list_generations_for_user,
)
from app.services.teacher.feedback_service import TeacherFeedbackService
from app.services.teacher.lesson_plan_service import TeacherLessonPlanService
from app.services.teacher.progress_service import TeacherProgressService
from app.services.teacher.quiz_service import TeacherQuizService
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/teacher",
    tags=["teacher"],
    dependencies=[Depends(require_role("teacher", "admin"))],
)


@router.post(
    "/lesson-notes",
    response_model=APIResponse[LessonNotesResponse],
    summary="Generate teacher-facing lesson notes for a topic",
    description=(
        "Drafts structured lesson notes (overview, sections, examples, "
        "teacher tips, common misconceptions) for a given subject, class "
        "level, and topic. Retrieval runs with role=teacher so the assistant "
        "draws on both student-visible textbooks and teacher-only material."
    ),
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def generate_lesson_notes(
    request: Request,
    body: LessonNotesRequest,
    service: TeacherLessonPlanService = Depends(get_lesson_plan_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[LessonNotesResponse]:
    payload = await service.generate(body, user_id=user.user_id)
    return success_response(
        role="teacher",
        data=payload,
        message=f"Lesson notes generated for '{body.topic}'.",
    )


@router.post(
    "/lesson-notes/stream",
    summary="Generate lesson notes (streaming)",
    description=(
        "Same body schema, role gate, and rate limit as "
        "``POST /teacher/lesson-notes``. Returns ``text/event-stream`` "
        "with three event types: ``meta`` (topic, subject, "
        "student_class, conversation_id — exactly enough for the UI "
        "shell), ``chunk`` (``{text}`` — raw LLM deltas, JSON tokens; "
        "frontends can render as a typing indicator OR ignore), and "
        "``done`` (``{result: LessonNotesResponse}`` — the parsed "
        "structured payload, identical to what the non-streaming "
        "endpoint returns, ready to render). An ``error`` event "
        "(``{code, message}``) replaces ``done`` on unrecoverable "
        "failure."
    ),
    responses={
        200: {
            "content": {SSE_MEDIA_TYPE: {}},
            "description": "SSE stream of ``meta`` / ``chunk`` / ``done`` frames.",
        }
    },
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def generate_lesson_notes_stream(
    request: Request,
    body: LessonNotesRequest,
    service: TeacherLessonPlanService = Depends(get_lesson_plan_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> StreamingResponse:
    async def _event_source():
        try:
            async for event in service.generate_stream(body, user_id=user.user_id):
                if isinstance(event, LessonNotesStreamMeta):
                    yield format_sse(
                        "meta",
                        {
                            "topic": event.topic,
                            "subject": event.subject,
                            "student_class": event.student_class,
                            "conversation_id": str(event.conversation_id),
                        },
                    )
                elif isinstance(event, LessonNotesStreamChunk):
                    yield format_sse("chunk", {"text": event.text})
                elif isinstance(event, LessonNotesStreamDone):
                    yield format_sse(
                        "done",
                        {"result": event.result.model_dump(mode="json")},
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("lesson_notes_stream failed: %s", exc)
            yield format_sse(
                "error",
                {
                    "code": "stream_failed",
                    "message": "The lesson-notes stream ended unexpectedly.",
                },
            )

    return StreamingResponse(
        _event_source(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_RESPONSE_HEADERS,
    )


@router.post(
    "/generate-content",
    summary="Generate teaching material as a streamed markdown document",
    description=(
        "Frontend-compatible content generator. Accepts the RevEd web app's "
        "existing payload (``contentType``, ``subject``, numeric "
        "``gradeLevel``, ``topic``, ``learningObjectives`` string, "
        "``difficultyLevel``, ``curriculumStandard``, ``tone``) and streams "
        "the result as **OpenAI-style** Server-Sent Events: lines of "
        "``data: {\"choices\":[{\"delta\":{\"content\":\"...\"}}]}`` "
        "terminated by ``data: [DONE]``. Supports all five content types "
        "(lesson_plan, quiz, notes, slides, study_guide). Unlike the "
        "frontend's previous generator, the output is grounded in the "
        "teacher corpus via RAG retrieval."
    ),
    responses={
        200: {
            "content": {SSE_MEDIA_TYPE: {}},
            "description": "OpenAI-style SSE stream of content deltas.",
        }
    },
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def generate_content(
    request: Request,
    body: TeacherContentRequest,
    service: TeacherContentService = Depends(get_content_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> StreamingResponse:
    async def _event_source():
        try:
            async for delta in service.generate_stream(body):
                yield format_openai_chunk(delta)
            yield OPENAI_SSE_DONE
        except Exception as exc:  # noqa: BLE001
            logger.exception("generate_content failed: %s", exc)
            yield format_openai_error(
                "AI generation failed. Please try again."
            )

    return StreamingResponse(
        _event_source(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_RESPONSE_HEADERS,
    )


@router.post(
    "/quiz",
    response_model=APIResponse[QuizResponse],
    summary="Generate a quiz with marking guide",
    description=(
        "Produces a quiz blueprint of N questions across an optional "
        "difficulty mix and question-type set, each with a marking guide "
        "(teacher-only material). Suitable for class assessments and "
        "homework."
    ),
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def generate_quiz(
    request: Request,
    body: QuizRequest,
    service: TeacherQuizService = Depends(get_quiz_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[QuizResponse]:
    payload = await service.generate(body, user_id=user.user_id)
    return success_response(
        role="teacher",
        data=payload,
        message=f"Quiz generated for '{body.topic}' ({len(payload.questions)} questions).",
    )


@router.post(
    "/student-feedback",
    response_model=APIResponse[FeedbackResponse],
    summary="Generate actionable feedback on a student submission",
    description=(
        "Given the question shown to the student and the student's answer "
        "(plus optional rubric), returns a structured critique with "
        "strengths, specific corrections, and recommended next steps."
    ),
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def generate_student_feedback(
    request: Request,
    body: FeedbackRequest,
    service: TeacherFeedbackService = Depends(get_feedback_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[FeedbackResponse]:
    payload = await service.generate(body, user_id=user.user_id)
    return success_response(
        role="teacher",
        data=payload,
        message="Feedback generated.",
    )


# --- Persisted generations ------------------------------------------------


@router.get(
    "/generations",
    response_model=APIResponse[TeacherGenerationListResponse],
    summary="List the caller's persisted teacher generations",
    description=(
        "Returns recent lesson notes, quizzes, and student-feedback artefacts "
        "the teacher has produced, newest first. Frontends use this to render a "
        "'Recent artefacts' sidebar. Optional filters: ``generation_type`` "
        "(lesson_notes | quiz | student_feedback) and ``conversation_id``."
    ),
)
async def list_teacher_generations(
    generation_type: str | None = Query(default=None),
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(
        default=None,
        description=(
            "Opaque pagination cursor from a prior response's ``next_cursor``. "
            "Omit on the first page."
        ),
    ),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[TeacherGenerationListResponse]:
    decoded = decode_cursor(cursor) if cursor else None
    rows, next_cursor = await list_generations_for_user(
        user_id=user.user_id,
        role="teacher",
        limit=clamp_limit(limit),
        generation_type=generation_type,
        conversation_id=conversation_id,
        cursor=decoded,
    )
    payload = TeacherGenerationListResponse(
        generations=[TeacherGenerationSummary(**r) for r in rows],
        next_cursor=next_cursor,
    )
    return success_response(
        role="teacher",
        data=payload,
        message=f"Found {len(rows)} generation(s).",
    )


@router.get(
    "/generations/{generation_id}",
    response_model=APIResponse[TeacherGenerationDetail],
    summary="Fetch one persisted generation by id",
    description=(
        "Returns the full request + response payloads for a previously-generated "
        "teacher artefact so the UI can re-render it. Access is restricted to "
        "the owning user."
    ),
)
async def get_teacher_generation(
    generation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[TeacherGenerationDetail]:
    row = await get_generation_for_user(
        generation_id=generation_id,
        user_id=user.user_id,
        role="teacher",
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found.",
        )
    payload = TeacherGenerationDetail(**row)
    return success_response(
        role="teacher",
        data=payload,
        message="Generation retrieved.",
    )


@router.get(
    "/class-progress",
    response_model=APIResponse[ClassProgressResponse],
    summary="Coarse class-activity summary for the requesting teacher",
    description=(
        "Phase 1 view: aggregates student-side questions over the recent "
        "period (default 14 days) into by-subject and by-class counts plus "
        "the top recurring topics. Per-student mastery and time-on-task "
        "land in a later phase when those signals are captured upstream."
    ),
)
async def get_class_progress(
    service: TeacherProgressService = Depends(get_progress_service),
    user: AuthenticatedUser = Depends(require_role("teacher", "admin")),
) -> APIResponse[ClassProgressResponse]:
    payload = await service.summarize(teacher_user_id=user.user_id)
    return success_response(
        role="teacher",
        data=payload,
        message=f"Summarised {payload.total_student_questions} student question(s).",
    )
