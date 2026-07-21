"""Admin — platform-wide usage + corpus stats."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import distinct, func, select

from app.core.config import Settings
from app.db.session import session_scope
from app.models.chat import ChatMessage
from app.models.parent import Parent
from app.models.school import School
from app.models.student import Student
from app.models.teacher import Teacher
from app.models.ai_generation import AIGeneration
from app.schemas.admin import ContentStatsResponse, UsageSummaryResponse

logger = logging.getLogger(__name__)


class AdminStatsService:
    """Aggregate platform stats — usage + corpus."""

    def __init__(
        self,
        *,
        settings: Settings,
        repo_root: Path | None = None,
        period_days: int = 30,
    ) -> None:
        self._settings = settings
        self._repo_root = repo_root or Path(__file__).resolve().parents[3]
        self._period_days = period_days

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        repo_root: Path | None = None,
    ) -> "AdminStatsService":
        return cls(settings=settings, repo_root=repo_root)

    # --- Usage ----------------------------------------------------------

    async def usage_summary(self) -> UsageSummaryResponse:
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=self._period_days)

        async with session_scope() as session:
            student_q_rows = (
                await session.execute(
                    select(ChatMessage.subject_hint, ChatMessage.user_id)
                    .where(
                        ChatMessage.created_at >= period_start,
                        ChatMessage.role == "student",
                    )
                )
            ).all()

            gen_rows = (
                await session.execute(
                    select(
                        AIGeneration.generation_type,
                        AIGeneration.role,
                        AIGeneration.user_id,
                    )
                    .where(AIGeneration.created_at >= period_start)
                )
            ).all()

            schools_count = await session.scalar(select(func.count()).select_from(School))
            teachers_count = await session.scalar(select(func.count()).select_from(Teacher))
            parents_count = await session.scalar(select(func.count()).select_from(Parent))
            students_count = await session.scalar(select(func.count()).select_from(Student))
            distinct_student_users = await session.scalar(
                select(func.count(distinct(ChatMessage.user_id))).where(
                    ChatMessage.created_at >= period_start,
                    ChatMessage.role == "student",
                    ChatMessage.user_id.is_not(None),
                )
            )
            distinct_generating_users = await session.scalar(
                select(func.count(distinct(AIGeneration.user_id))).where(
                    AIGeneration.created_at >= period_start,
                    AIGeneration.user_id.is_not(None),
                )
            )

        by_subject: dict[str, int] = {}
        for subj, _ in student_q_rows:
            if subj:
                by_subject[subj] = by_subject.get(subj, 0) + 1

        by_gen_type: dict[str, int] = {}
        by_role: dict[str, int] = {}
        for gen_type, role, _ in gen_rows:
            by_gen_type[gen_type] = by_gen_type.get(gen_type, 0) + 1
            by_role[role] = by_role.get(role, 0) + 1

        return UsageSummaryResponse(
            period_start=period_start,
            period_end=period_end,
            total_student_questions=len(student_q_rows),
            total_ai_generations=len(gen_rows),
            generations_by_role=dict(
                sorted(by_role.items(), key=lambda kv: -kv[1])
            ),
            questions_by_subject=dict(
                sorted(by_subject.items(), key=lambda kv: -kv[1])
            ),
            generations_by_type=dict(
                sorted(by_gen_type.items(), key=lambda kv: -kv[1])
            ),
            distinct_student_users=int(distinct_student_users or 0),
            distinct_generating_users=int(distinct_generating_users or 0),
            schools=int(schools_count or 0),
            teachers=int(teachers_count or 0),
            parents=int(parents_count or 0),
            students=int(students_count or 0),
        )

    # --- Content (corpus) -----------------------------------------------

    async def content_stats(self) -> ContentStatsResponse:
        # Pinecone vector count.
        pinecone_count = 0
        try:
            from pinecone import Pinecone  # type: ignore[import-not-found]
            pc = Pinecone(api_key=self._settings.pinecone_api_key)
            idx = pc.Index(name=self._settings.pinecone_index_name)
            stats = idx.describe_index_stats()
            namespaces = (
                stats.get("namespaces") if isinstance(stats, dict)
                else getattr(stats, "namespaces", {})
            ) or {}
            ns_info = namespaces.get(self._settings.pinecone_namespace)
            if ns_info:
                pinecone_count = int(
                    ns_info.get("vector_count")
                    if isinstance(ns_info, dict)
                    else getattr(ns_info, "vector_count", 0)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pinecone stats unavailable: %s", exc)

        # On-disk JSONL stats.
        chunks_root = self._repo_root / "data" / "chunks"
        files = 0
        total_chunks = 0
        by_ct: dict[str, int] = {}
        by_subj: dict[str, int] = {}

        if chunks_root.exists():
            for path in chunks_root.rglob("*.jsonl"):
                if "v1_backup" in path.as_posix():
                    continue
                # path layout: data/chunks/<content_type>/<subject>/<file>.jsonl
                parts = path.relative_to(chunks_root).parts
                content_type = parts[0] if len(parts) >= 2 else "unknown"
                subject = parts[1] if len(parts) >= 3 else "unknown"
                files += 1
                count = 0
                try:
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            if line.strip():
                                count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not count chunks in %s: %s", path, exc)
                    continue
                total_chunks += count
                by_ct[content_type] = by_ct.get(content_type, 0) + count
                by_subj[subject] = by_subj.get(subject, 0) + count

        return ContentStatsResponse(
            pinecone_index=self._settings.pinecone_index_name,
            pinecone_namespace=self._settings.pinecone_namespace,
            pinecone_dimension=self._settings.pinecone_dimension,
            pinecone_vector_count=pinecone_count,
            on_disk_chunk_files=files,
            on_disk_chunks_total=total_chunks,
            chunks_by_content_type=dict(
                sorted(by_ct.items(), key=lambda kv: -kv[1])
            ),
            chunks_by_subject=dict(
                sorted(by_subj.items(), key=lambda kv: -kv[1])
            ),
        )
