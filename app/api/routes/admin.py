"""Admin API routes.

All endpoints require ``role=admin``. In dev mode use ``X-Dev-Role: admin``.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import (
    get_admin_provisioning_service,
    get_admin_stats_service,
    get_notification_service,
)
from app.core.audit import log_auth_event
from app.core.security import AuthenticatedUser, require_role
from app.schemas.admin import (
    ClassRosterRequest,
    ClassRosterResponse,
    ContentStatsResponse,
    ParentSetupRequest,
    ParentSetupResponse,
    TeacherSetupRequest,
    TeacherSetupResponse,
    UsageSummaryResponse,
)
from app.schemas.common import APIResponse
from app.schemas.notification import CreateNotificationRequest, NotificationOut
from app.services.admin.analytics_service import AdminStatsService
from app.services.admin.approval_service import AdminProvisioningService
from app.services.notification_service import NotificationService
from app.utils.response_builder import success_response

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_role("admin"))],
)


# --- Provisioning --------------------------------------------------------


@router.post(
    "/teachers/setup",
    response_model=APIResponse[TeacherSetupResponse],
    summary="Provision a teacher (school + teacher row + classes)",
    description=(
        "One-shot provisioning: creates or updates the school, links a "
        "Supabase auth user_id to a Teacher row, and creates the classes "
        "the teacher will teach. Idempotent — re-running with the same "
        "supabase_user_id updates the existing teacher; classes match by "
        "(school_id, teacher_id, name). Used to enable real teacher-class "
        "scoping for /teacher/class-progress."
    ),
)
async def setup_teacher(
    body: TeacherSetupRequest,
    service: AdminProvisioningService = Depends(get_admin_provisioning_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[TeacherSetupResponse]:
    payload = await service.setup_teacher(
        body, caller_user_id=user.user_id, is_dev_stub=user.is_stub
    )
    log_auth_event(
        event="admin_action",
        outcome="success",
        user_id=user.user_id,
        role="admin",
        endpoint="POST /api/v1/admin/teachers/setup",
        extra={
            "action": "teacher_setup",
            "school_id": payload.school_id,
            "teacher_id": payload.teacher_id,
            "class_ids": payload.class_ids,
        },
    )
    return success_response(role="admin", data=payload, message=payload.message)


@router.post(
    "/classes/{class_id}/roster",
    response_model=APIResponse[ClassRosterResponse],
    summary="Enrol students into a class",
    description=(
        "Add students to a SchoolClass roster. Accepts either ``student_ids`` "
        "(direct Student.id values) or ``student_supabase_user_ids`` (the "
        "service resolves to Student rows). Idempotent — re-running with the "
        "same students is a no-op. After enrolment, /teacher/class-progress "
        "for the class's teacher uses the roster for scoping."
    ),
)
async def update_class_roster(
    class_id: uuid.UUID,
    body: ClassRosterRequest,
    service: AdminProvisioningService = Depends(get_admin_provisioning_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[ClassRosterResponse]:
    try:
        payload = await service.update_class_roster(
            class_id=class_id,
            request=body,
            caller_user_id=user.user_id,
            is_dev_stub=user.is_stub,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    log_auth_event(
        event="admin_action",
        outcome="success",
        user_id=user.user_id,
        role="admin",
        endpoint=f"POST /api/v1/admin/classes/{class_id}/roster",
        extra={
            "action": "roster_update",
            "class_id": class_id,
            "added_count": len(payload.added),
            "skipped_count": len(payload.skipped_user_ids),
            "total_in_class": payload.total_in_class,
        },
    )
    return success_response(role="admin", data=payload, message=payload.message)


@router.post(
    "/parents/setup",
    response_model=APIResponse[ParentSetupResponse],
    summary="Provision a parent (parent row + linked children)",
    description=(
        "One-shot provisioning: links a Supabase auth user_id to a Parent "
        "row and creates Student rows for each child with the parent link. "
        "Used to enable /parent/child-activity."
    ),
)
async def setup_parent(
    body: ParentSetupRequest,
    service: AdminProvisioningService = Depends(get_admin_provisioning_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[ParentSetupResponse]:
    payload = await service.setup_parent(body)
    log_auth_event(
        event="admin_action",
        outcome="success",
        user_id=user.user_id,
        role="admin",
        endpoint="POST /api/v1/admin/parents/setup",
        extra={
            "action": "parent_setup",
            "parent_id": payload.parent_id,
            "student_ids": payload.student_ids,
        },
    )
    return success_response(role="admin", data=payload, message=payload.message)


# --- Stats ---------------------------------------------------------------


@router.get(
    "/usage-summary",
    response_model=APIResponse[UsageSummaryResponse],
    summary="Platform-wide usage stats over the recent period",
)
async def usage_summary(
    service: AdminStatsService = Depends(get_admin_stats_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[UsageSummaryResponse]:
    payload = await service.usage_summary()
    return success_response(
        role="admin",
        data=payload,
        message=(
            f"{payload.total_student_questions} student question(s); "
            f"{payload.total_ai_generations} AI generation(s)."
        ),
    )


@router.get(
    "/content-stats",
    response_model=APIResponse[ContentStatsResponse],
    summary="Corpus stats — Pinecone vector count and on-disk chunk counts",
)
async def content_stats(
    service: AdminStatsService = Depends(get_admin_stats_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[ContentStatsResponse]:
    payload = await service.content_stats()
    return success_response(
        role="admin",
        data=payload,
        message=(
            f"{payload.pinecone_vector_count} vector(s) across "
            f"{payload.on_disk_chunk_files} chunk file(s)."
        ),
    )


@router.post(
    "/notifications",
    response_model=APIResponse[NotificationOut],
    summary="Deliver a notification to a user (admin-only)",
    description=(
        "Creates a notification visible to the specified user via their "
        "``GET /notifications`` feed. Use for system messages, performance "
        "alerts, schedule reminders, etc. Cross-role: an admin can target "
        "any role's user."
    ),
)
async def create_notification(
    body: CreateNotificationRequest,
    service: NotificationService = Depends(get_notification_service),
    user: AuthenticatedUser = Depends(require_role("admin")),
) -> APIResponse[NotificationOut]:
    row = await service.create(body)
    log_auth_event(
        event="admin_action",
        outcome="success",
        user_id=user.user_id,
        role="admin",
        endpoint="POST /api/v1/admin/notifications",
        extra={
            "action": "notification_create",
            "notification_id": row.id,
            "recipient_user_id": row.recipient_user_id,
            "recipient_role": row.recipient_role,
            "category": row.category,
        },
    )
    payload = NotificationOut(
        id=row.id,
        recipient_user_id=row.recipient_user_id,
        recipient_role=row.recipient_role,  # type: ignore[arg-type]
        category=row.category,
        title=row.title,
        body=row.body,
        payload=row.payload,
        is_read=row.is_read,
        read_at=row.read_at,
        created_at=row.created_at,
    )
    return success_response(
        role="admin",
        data=payload,
        message=(
            f"Notification delivered to {body.recipient_user_id} ({body.recipient_role})."
        ),
    )
