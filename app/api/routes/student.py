"""Student-facing API routes.

All student endpoints return the standard RevEd envelope
``{"status", "data", "message", "role"}`` so the frontend can parse every
response the same way.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api._pagination import clamp_limit, decode_cursor
from app.api._sse import SSE_MEDIA_TYPE, SSE_RESPONSE_HEADERS, format_sse
from app.api.dependencies import (
    get_career_service,
    get_goal_service,
    get_learning_path_service,
    get_study_group_service,
    get_tutor_service,
)
from app.core.rate_limit import limiter, llm_limit_for_key, tiered_rate_limit_key
from app.core.security import AuthenticatedUser, require_role
from app.services.student.tutor_service import (
    TutorStreamChunk,
    TutorStreamDone,
    TutorStreamMeta,
)
from app.schemas.common import (
    AIGenerationDetail,
    AIGenerationListResponse,
    AIGenerationSummary,
    APIResponse,
)
from app.schemas.student import (
    CareerGuidanceRequest,
    CareerGuidanceResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSummary,
    ConversationTurn,
    GoalCreateRequest,
    GoalListResponse,
    GoalProgressUpdateRequest,
    GoalResponse,
    LearningPathRequest,
    LearningPathResponse,
    StudentAnswerResponse,
    StudentQuestionRequest,
    StudyGroupCreateRequest,
    StudyGroupDiscussionRequest,
    StudyGroupDiscussionResponse,
    StudyGroupJoinRequest,
    StudyGroupListResponse,
    StudyGroupResponse,
)
from app.services.student.career_service import StudentCareerService
from app.services.student.goal_service import StudentGoalService
from app.services.student.learning_path_service import StudentLearningPathService
from app.services.student.study_group_service import StudentStudyGroupService
from app.services.student.tutor_service import StudentTutorService
from app.services.teacher._persistence import (
    get_generation_for_user,
    list_generations_for_user,
)
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/student",
    tags=["student"],
    dependencies=[Depends(require_role("student"))],
)


@router.post(
    "/ask",
    response_model=APIResponse[StudentAnswerResponse],
    summary="Ask a student question",
    description=(
        "Submit a student question and receive a class-aware, "
        "teacher-style grounded answer."
    ),
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def ask_question(
    request: Request,
    body: StudentQuestionRequest,
    tutor_service: StudentTutorService = Depends(get_tutor_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[StudentAnswerResponse]:
    result = await tutor_service.ask(
        question=body.question,
        student_class=body.student_class,
        subject=body.subject,
        history=body.history,
        learning_state=body.learning_state,
        user_id=user.user_id,
        conversation_id=body.conversation_id,
    )
    payload = StudentAnswerResponse(
        status=result.status,
        answer=result.answer,
        student_class=result.student_class,
        subject=result.subject,
        original_question=result.original_question,
        corrected_question=result.corrected_question,
        original_subject=result.original_subject,
        clarifying_question=result.clarifying_question,
        conversation_id=result.conversation_id,
    )
    message = (
        "Answer generated."
        if result.status == "answered"
        else "Clarification needed."
    )
    return success_response(
        role="student",
        data=payload,
        message=message,
    )


@router.post(
    "/ask/stream",
    summary="Ask a student question (streaming)",
    description=(
        "Same contract as ``POST /student/ask`` but returns ``text/event-"
        "stream`` so the frontend can render the answer as it's generated. "
        "Three event types: ``meta`` (status, subject, conversation_id, "
        "did-you-mean fields — exactly the non-text fields of "
        "``StudentAnswerResponse``), ``chunk`` (``{text: ...}``, repeated), "
        "and ``done`` (``{final_answer: string | null}`` — when non-null, "
        "the frontend should replace the streamed text with this guarded "
        "version). Same role gate and rate limit as ``/ask``; same body "
        "schema. Client disconnect cancels the upstream Groq stream — no "
        "tokens billed for delivery the client never received."
    ),
    responses={
        200: {
            "content": {SSE_MEDIA_TYPE: {}},
            "description": "Event stream of ``meta`` / ``chunk`` / ``done`` frames.",
        }
    },
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def ask_question_stream(
    request: Request,
    body: StudentQuestionRequest,
    tutor_service: StudentTutorService = Depends(get_tutor_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> StreamingResponse:
    async def _event_source():
        try:
            async for event in tutor_service.ask_stream(
                question=body.question,
                student_class=body.student_class,
                subject=body.subject,
                history=body.history,
                learning_state=body.learning_state,
                user_id=user.user_id,
                conversation_id=body.conversation_id,
            ):
                if isinstance(event, TutorStreamMeta):
                    yield format_sse(
                        "meta",
                        {
                            "status": event.status,
                            "student_class": event.student_class,
                            "subject": event.subject,
                            "conversation_id": (
                                str(event.conversation_id)
                                if event.conversation_id is not None
                                else None
                            ),
                            "original_question": event.original_question,
                            "corrected_question": event.corrected_question,
                            "original_subject": event.original_subject,
                            "clarifying_question": event.clarifying_question,
                        },
                    )
                elif isinstance(event, TutorStreamChunk):
                    yield format_sse("chunk", {"text": event.text})
                elif isinstance(event, TutorStreamDone):
                    yield format_sse(
                        "done", {"final_answer": event.final_answer}
                    )
        except Exception as exc:  # noqa: BLE001
            # Surface backend errors as a final SSE ``error`` event
            # rather than aborting the stream silently — the frontend
            # can render a "something went wrong" toast and bail out.
            # We never include exception details in the wire payload.
            logger.exception("ask_stream failed: %s", exc)
            yield format_sse(
                "error",
                {
                    "code": "stream_failed",
                    "message": "The answer stream ended unexpectedly.",
                },
            )

    return StreamingResponse(
        _event_source(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/conversations",
    response_model=APIResponse[ConversationListResponse],
    summary="List the caller's tutor conversation threads",
    description=(
        "Returns the caller's conversation threads, newest first. Frontends "
        "use this to render a 'recent chats' sidebar. Pass the chosen "
        "``conversation_id`` to ``GET /student/conversations/{id}/history`` "
        "to rehydrate the turns for replay on the next /ask call."
    ),
)
async def list_conversations(
    tutor_service: StudentTutorService = Depends(get_tutor_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[ConversationListResponse]:
    items = await tutor_service.list_conversations(user_id=user.user_id)
    payload = ConversationListResponse(
        conversations=[ConversationSummary(**item) for item in items],
    )
    return success_response(
        role="student",
        data=payload,
        message=f"Found {len(items)} conversation(s).",
    )


@router.get(
    "/conversations/{conversation_id}/history",
    response_model=APIResponse[ConversationHistoryResponse],
    summary="Fetch ordered turns for one conversation",
    description=(
        "Returns the ordered user/assistant turns for a single conversation. "
        "Frontends should plug the result into the ``history`` field on the "
        "next ``POST /student/ask`` call to continue the thread. Access is "
        "restricted to the conversation's owning user — mismatched callers "
        "receive an empty turn list (identical to 'no such conversation' to "
        "avoid leaking thread existence)."
    ),
)
async def get_conversation_history(
    conversation_id: uuid.UUID,
    tutor_service: StudentTutorService = Depends(get_tutor_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[ConversationHistoryResponse]:
    turns = await tutor_service.conversation_history(
        conversation_id=conversation_id,
        user_id=user.user_id,
    )
    payload = ConversationHistoryResponse(
        conversation_id=conversation_id,
        turns=turns,
    )
    return success_response(
        role="student",
        data=payload,
        message=f"Retrieved {len(turns)} turn(s).",
    )


@router.post(
    "/learning-path",
    response_model=APIResponse[LearningPathResponse],
    summary="Generate a personalized learning pathway",
)
async def generate_learning_path(
    body: LearningPathRequest,
    service: StudentLearningPathService = Depends(get_learning_path_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[LearningPathResponse]:
    payload = await service.generate_path(
        student_class=body.student_class,
        subject=body.subject,
        topic=body.topic,
        current_understanding=body.current_understanding,
        weekly_study_hours=body.weekly_study_hours,
        user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Learning pathway generated.",
    )


@router.post(
    "/goals",
    response_model=APIResponse[GoalResponse],
    summary="Create a student learning goal",
)
async def create_goal(
    body: GoalCreateRequest,
    service: StudentGoalService = Depends(get_goal_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[GoalResponse]:
    payload = await service.create_goal(
        student_id=body.student_id,
        title=body.title,
        description=body.description,
        subject=body.subject,
        target_date=body.target_date,
        caller_user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Goal created.",
    )


@router.get(
    "/goals/{student_id}",
    response_model=APIResponse[GoalListResponse],
    summary="List a student's learning goals",
    description=(
        "Returns goals for the given ``student_id``. The caller must own "
        "that Student row — mismatches return 404 (deliberately identical "
        "to 'not found' so other students' UUIDs cannot be enumerated)."
    ),
)
async def list_goals(
    student_id: str,
    service: StudentGoalService = Depends(get_goal_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[GoalListResponse]:
    payload = await service.list_goals(
        student_id=student_id, caller_user_id=user.user_id
    )
    return success_response(
        role="student",
        data=payload,
        message="Goals retrieved.",
    )


@router.patch(
    "/goals/{goal_id}/progress",
    response_model=APIResponse[GoalResponse],
    summary="Update progress on a learning goal",
    description=(
        "Updates progress on a goal the caller owns. Mismatches return "
        "404 — the same response shape as 'goal does not exist' so "
        "callers cannot probe for valid UUIDs."
    ),
)
async def update_goal_progress(
    goal_id: str,
    body: GoalProgressUpdateRequest,
    service: StudentGoalService = Depends(get_goal_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[GoalResponse]:
    payload = await service.update_progress(
        goal_id=goal_id,
        progress_percent=body.progress_percent,
        note=body.note,
        caller_user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Goal progress updated.",
    )


@router.post(
    "/study-groups",
    response_model=APIResponse[StudyGroupResponse],
    summary="Create a collaborative study group",
    description=(
        "Creates a study group with the caller as the founding member. "
        "``creator_student_id`` must resolve to the caller's own Student "
        "row — mismatches return 404."
    ),
)
async def create_study_group(
    body: StudyGroupCreateRequest,
    service: StudentStudyGroupService = Depends(get_study_group_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[StudyGroupResponse]:
    payload = await service.create_group(
        creator_student_id=body.creator_student_id,
        name=body.name,
        subject=body.subject,
        topic=body.topic,
        student_class=body.student_class,
        caller_user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Study group created.",
    )


@router.post(
    "/study-groups/{group_id}/join",
    response_model=APIResponse[StudyGroupResponse],
    summary="Join an existing study group",
    description=(
        "Adds the caller to a study group. ``student_id`` in the body "
        "must resolve to the caller's own Student row — anyone joining "
        "on behalf of another student is rejected with 404."
    ),
)
async def join_study_group(
    group_id: str,
    body: StudyGroupJoinRequest,
    service: StudentStudyGroupService = Depends(get_study_group_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[StudyGroupResponse]:
    payload = await service.join_group(
        group_id=group_id,
        student_id=body.student_id,
        caller_user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Joined study group.",
    )


@router.get(
    "/study-groups",
    response_model=APIResponse[StudyGroupListResponse],
    summary="List study groups for a class or subject",
)
async def list_study_groups(
    student_class: str | None = None,
    subject: str | None = None,
    service: StudentStudyGroupService = Depends(get_study_group_service),
) -> APIResponse[StudyGroupListResponse]:
    payload = await service.list_groups(student_class=student_class, subject=subject)
    return success_response(
        role="student",
        data=payload,
        message="Study groups retrieved.",
    )


@router.post(
    "/study-groups/{group_id}/facilitate",
    response_model=APIResponse[StudyGroupDiscussionResponse],
    summary="Facilitate group study with AI-generated discussion prompts",
    description=(
        "Generates AI-facilitated discussion prompts for a group. The "
        "caller must be a member of the group — non-members get 404 to "
        "avoid leaking which groups exist."
    ),
)
async def facilitate_study_group(
    group_id: str,
    body: StudyGroupDiscussionRequest,
    service: StudentStudyGroupService = Depends(get_study_group_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[StudyGroupDiscussionResponse]:
    payload = await service.facilitate(
        group_id=group_id,
        focus_question=body.focus_question,
        caller_user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Group discussion facilitated.",
    )


@router.post(
    "/career-guidance",
    response_model=APIResponse[CareerGuidanceResponse],
    summary="Get AI-driven career guidance aligned to the learner profile",
)
async def career_guidance(
    body: CareerGuidanceRequest,
    service: StudentCareerService = Depends(get_career_service),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[CareerGuidanceResponse]:
    payload = await service.suggest_paths(
        student_class=body.student_class,
        favorite_subjects=body.favorite_subjects,
        strengths=body.strengths,
        interests=body.interests,
        long_term_dream=body.long_term_dream,
        user_id=user.user_id,
    )
    return success_response(
        role="student",
        data=payload,
        message="Career guidance generated.",
    )


# --- Persisted student generations (learning_path, career_guidance) ----


@router.get(
    "/generations",
    response_model=APIResponse[AIGenerationListResponse],
    summary="List the caller's persisted student AI generations",
    description=(
        "Returns recent learning-path and career-guidance artefacts the "
        "student has produced, newest first. The student tutor's per-turn "
        "Q&A history lives separately under ``/student/conversations``."
    ),
)
async def list_student_generations(
    generation_type: str | None = None,
    limit: int = 50,
    cursor: str | None = Query(
        default=None,
        description=(
            "Opaque pagination cursor from a prior response's ``next_cursor``. "
            "Omit on the first page."
        ),
    ),
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[AIGenerationListResponse]:
    decoded = decode_cursor(cursor) if cursor else None
    rows, next_cursor = await list_generations_for_user(
        user_id=user.user_id,
        role="student",
        limit=clamp_limit(limit),
        generation_type=generation_type,
        cursor=decoded,
    )
    payload = AIGenerationListResponse(
        generations=[AIGenerationSummary(**r) for r in rows],
        next_cursor=next_cursor,
    )
    return success_response(
        role="student",
        data=payload,
        message=f"Found {len(rows)} generation(s).",
    )


@router.get(
    "/generations/{generation_id}",
    response_model=APIResponse[AIGenerationDetail],
    summary="Fetch one persisted student generation by id",
)
async def get_student_generation(
    generation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(require_role("student")),
) -> APIResponse[AIGenerationDetail]:
    from fastapi import HTTPException, status
    row = await get_generation_for_user(
        generation_id=generation_id,
        user_id=user.user_id,
        role="student",
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found.",
        )
    payload = AIGenerationDetail(**row)
    return success_response(
        role="student",
        data=payload,
        message="Generation retrieved.",
    )
