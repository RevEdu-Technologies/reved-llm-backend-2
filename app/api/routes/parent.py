"""Parent API routes.

All endpoints require ``role=parent`` (or ``admin``). In dev mode use
``X-Dev-Role: parent`` from the Authorize dialog in Swagger.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api._pagination import clamp_limit, decode_cursor
from app.api._sse import SSE_MEDIA_TYPE, SSE_RESPONSE_HEADERS, format_sse
from app.api.dependencies import (
    get_parent_activity_service,
    get_parent_explain_service,
)
from app.core.rate_limit import limiter, llm_limit_for_key, tiered_rate_limit_key
from app.core.security import AuthenticatedUser, require_role
from app.services.parent.communication_service import (
    ExplainTopicStreamChunk,
    ExplainTopicStreamDone,
    ExplainTopicStreamMeta,
)
from app.schemas.common import (
    AIGenerationDetail,
    AIGenerationListResponse,
    AIGenerationSummary,
    APIResponse,
)
from app.schemas.parent import (
    ChildActivityResponse,
    ExplainTopicRequest,
    ExplainTopicResponse,
)
from app.services.parent.communication_service import ParentExplainService
from app.services.parent.report_service import ParentActivityService
from app.services.teacher._persistence import (
    get_generation_for_user,
    list_generations_for_user,
)
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/parent",
    tags=["parent"],
    dependencies=[Depends(require_role("parent", "admin"))],
)


@router.post(
    "/explain-topic",
    response_model=APIResponse[ExplainTopicResponse],
    summary="Plain-language explanation of a topic for a parent",
    description=(
        "Returns a parent-friendly explanation of a topic the parent's child "
        "is studying — including an everyday analogy and 2-4 things they can "
        "try together at home. Retrieval runs with role=parent, so only "
        "student-visible material is consulted (no teacher-only contexts)."
    ),
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def explain_topic(
    request: Request,
    body: ExplainTopicRequest,
    service: ParentExplainService = Depends(get_parent_explain_service),
    user: AuthenticatedUser = Depends(require_role("parent", "admin")),
) -> APIResponse[ExplainTopicResponse]:
    payload = await service.explain(body, user_id=user.user_id)
    return success_response(
        role="parent",
        data=payload,
        message=f"Topic explained: '{body.topic}'.",
    )


@router.post(
    "/explain-topic/stream",
    summary="Plain-language explanation of a topic (streaming)",
    description=(
        "Same body schema, role gate, and rate limit as "
        "``POST /parent/explain-topic``. Returns ``text/event-stream`` "
        "with three event types: ``meta`` (topic, subject, "
        "student_class), ``chunk`` (``{text}`` — raw LLM deltas, JSON "
        "tokens), and ``done`` (``{result: ExplainTopicResponse}`` — "
        "the parsed payload ready to render). An ``error`` event "
        "replaces ``done`` on unrecoverable failure."
    ),
    responses={
        200: {
            "content": {SSE_MEDIA_TYPE: {}},
            "description": "SSE stream of ``meta`` / ``chunk`` / ``done`` frames.",
        }
    },
)
@limiter.limit(llm_limit_for_key, key_func=tiered_rate_limit_key)
async def explain_topic_stream(
    request: Request,
    body: ExplainTopicRequest,
    service: ParentExplainService = Depends(get_parent_explain_service),
    user: AuthenticatedUser = Depends(require_role("parent", "admin")),
) -> StreamingResponse:
    async def _event_source():
        try:
            async for event in service.explain_stream(body, user_id=user.user_id):
                if isinstance(event, ExplainTopicStreamMeta):
                    yield format_sse(
                        "meta",
                        {
                            "topic": event.topic,
                            "subject": event.subject,
                            "student_class": event.student_class,
                        },
                    )
                elif isinstance(event, ExplainTopicStreamChunk):
                    yield format_sse("chunk", {"text": event.text})
                elif isinstance(event, ExplainTopicStreamDone):
                    yield format_sse(
                        "done",
                        {"result": event.result.model_dump(mode="json")},
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("explain_topic_stream failed: %s", exc)
            yield format_sse(
                "error",
                {
                    "code": "stream_failed",
                    "message": "The explanation stream ended unexpectedly.",
                },
            )

    return StreamingResponse(
        _event_source(),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_RESPONSE_HEADERS,
    )


@router.get(
    "/child-activity",
    response_model=APIResponse[ChildActivityResponse],
    summary="Recent learning activity for the parent's linked children",
    description=(
        "Aggregates each linked child's recent student-tutor activity "
        "(questions by subject, recent question previews). A parent with no "
        "linked Parent row yet returns an empty list — frontends should "
        "show an onboarding prompt in that case."
    ),
)
async def child_activity(
    service: ParentActivityService = Depends(get_parent_activity_service),
    user: AuthenticatedUser = Depends(require_role("parent", "admin")),
) -> APIResponse[ChildActivityResponse]:
    payload = await service.summarize(parent_user_id=user.user_id)
    return success_response(
        role="parent",
        data=payload,
        message=f"Summarised activity for {len(payload.children)} child(ren).",
    )


# --- Persisted parent generations (explain_topic) ----


@router.get(
    "/generations",
    response_model=APIResponse[AIGenerationListResponse],
    summary="List the caller's persisted parent AI generations",
)
async def list_parent_generations(
    generation_type: str | None = None,
    limit: int = 50,
    cursor: str | None = Query(
        default=None,
        description=(
            "Opaque pagination cursor from a prior response's ``next_cursor``. "
            "Omit on the first page."
        ),
    ),
    user: AuthenticatedUser = Depends(require_role("parent", "admin")),
) -> APIResponse[AIGenerationListResponse]:
    decoded = decode_cursor(cursor) if cursor else None
    rows, next_cursor = await list_generations_for_user(
        user_id=user.user_id,
        role="parent",
        limit=clamp_limit(limit),
        generation_type=generation_type,
        cursor=decoded,
    )
    payload = AIGenerationListResponse(
        generations=[AIGenerationSummary(**r) for r in rows],
        next_cursor=next_cursor,
    )
    return success_response(
        role="parent",
        data=payload,
        message=f"Found {len(rows)} generation(s).",
    )


@router.get(
    "/generations/{generation_id}",
    response_model=APIResponse[AIGenerationDetail],
    summary="Fetch one persisted parent generation by id",
)
async def get_parent_generation(
    generation_id: uuid.UUID,
    user: AuthenticatedUser = Depends(require_role("parent", "admin")),
) -> APIResponse[AIGenerationDetail]:
    row = await get_generation_for_user(
        generation_id=generation_id,
        user_id=user.user_id,
        role="parent",
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Generation not found.",
        )
    payload = AIGenerationDetail(**row)
    return success_response(
        role="parent",
        data=payload,
        message="Generation retrieved.",
    )
