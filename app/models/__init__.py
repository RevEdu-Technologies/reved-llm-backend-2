"""ORM model registry. Importing this package registers all tables on Base.metadata."""

from app.models.admin import Admin
from app.models.ai_generation import AIGeneration
from app.models.chat import ChatMessage
from app.models.notification import Notification
from app.models.parent import Parent
from app.models.school import School, SchoolClass
from app.models.student import (
    Goal,
    PersonalizedAIProfile,
    Student,
    StudyGroup,
    study_group_members,
)
from app.models.student_class_membership import StudentClassMembership
from app.models.teacher import Teacher
from app.models.teacher_generation import TeacherGeneration
from app.models.webhook import WebhookDelivery, WebhookSubscription

__all__ = [
    "Admin",
    "AIGeneration",
    "ChatMessage",
    "Goal",
    "Notification",
    "Parent",
    "PersonalizedAIProfile",
    "School",
    "SchoolClass",
    "Student",
    "StudentClassMembership",
    "StudyGroup",
    "Teacher",
    "TeacherGeneration",
    "WebhookDelivery",
    "WebhookSubscription",
    "study_group_members",
]
