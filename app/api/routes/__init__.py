"""Route registry for the API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.health import router as health_router
from app.api.routes.notifications import router as notifications_router
from app.api.routes.parent import router as parent_router
from app.api.routes.student import router as student_router
from app.api.routes.teacher import router as teacher_router
from app.api.routes.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(student_router)
api_router.include_router(teacher_router)
api_router.include_router(parent_router)
api_router.include_router(admin_router)
api_router.include_router(notifications_router)
api_router.include_router(webhooks_router)
