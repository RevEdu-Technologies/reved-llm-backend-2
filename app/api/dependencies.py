"""Shared FastAPI dependencies for route handlers.

Each student service is cached so Pinecone, HuggingFace, and Groq clients
are constructed once per process. Services with persistence (goals, study
groups) use in-memory repositories in Phase 1 — keeping the service as a
singleton means the repo state is preserved across requests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.services.student.career_service import StudentCareerService
from app.services.student.goal_service import StudentGoalService
from app.services.student.learning_path_service import StudentLearningPathService
from app.services.student.study_group_service import StudentStudyGroupService
from app.services.student.tutor_service import StudentTutorService
from app.services.teacher.content_service import TeacherContentService
from app.services.teacher.feedback_service import TeacherFeedbackService
from app.services.teacher.lesson_plan_service import TeacherLessonPlanService
from app.services.teacher.progress_service import TeacherProgressService
from app.services.teacher.quiz_service import TeacherQuizService
from app.services.parent.communication_service import ParentExplainService
from app.services.parent.report_service import ParentActivityService
from app.services.admin.analytics_service import AdminStatsService
from app.services.admin.approval_service import AdminProvisioningService
from app.services.notification_service import NotificationService
from app.services.webhook_service import WebhookService

_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_tutor_service() -> StudentTutorService:
    """Return a cached StudentTutorService instance."""

    return StudentTutorService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_learning_path_service() -> StudentLearningPathService:
    """Return a cached StudentLearningPathService instance."""

    return StudentLearningPathService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_goal_service() -> StudentGoalService:
    """Return a cached StudentGoalService instance."""

    return StudentGoalService.from_settings(get_settings())


@lru_cache(maxsize=1)
def get_study_group_service() -> StudentStudyGroupService:
    """Return a cached StudentStudyGroupService instance."""

    return StudentStudyGroupService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_career_service() -> StudentCareerService:
    """Return a cached StudentCareerService instance."""

    return StudentCareerService.from_settings(get_settings(), repo_root=_REPO_ROOT)


# --- Teacher Copilot services --------------------------------------------


@lru_cache(maxsize=1)
def get_lesson_plan_service() -> TeacherLessonPlanService:
    """Return a cached TeacherLessonPlanService instance."""

    return TeacherLessonPlanService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_quiz_service() -> TeacherQuizService:
    """Return a cached TeacherQuizService instance."""

    return TeacherQuizService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_content_service() -> TeacherContentService:
    """Return a cached TeacherContentService instance."""

    return TeacherContentService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_feedback_service() -> TeacherFeedbackService:
    """Return a cached TeacherFeedbackService instance."""

    return TeacherFeedbackService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_progress_service() -> TeacherProgressService:
    """Return a cached TeacherProgressService instance."""

    return TeacherProgressService.from_settings(get_settings())


# --- Parent services -----------------------------------------------------


@lru_cache(maxsize=1)
def get_parent_explain_service() -> ParentExplainService:
    return ParentExplainService.from_settings(get_settings(), repo_root=_REPO_ROOT)


@lru_cache(maxsize=1)
def get_parent_activity_service() -> ParentActivityService:
    return ParentActivityService.from_settings(get_settings())


# --- Admin services ------------------------------------------------------


@lru_cache(maxsize=1)
def get_admin_provisioning_service() -> AdminProvisioningService:
    return AdminProvisioningService.from_settings(get_settings())


@lru_cache(maxsize=1)
def get_admin_stats_service() -> AdminStatsService:
    return AdminStatsService.from_settings(get_settings(), repo_root=_REPO_ROOT)


# --- Cross-role services -------------------------------------------------


@lru_cache(maxsize=1)
def get_notification_service() -> NotificationService:
    return NotificationService.from_settings(get_settings())


@lru_cache(maxsize=1)
def get_webhook_service() -> WebhookService:
    return WebhookService.from_settings(get_settings())
