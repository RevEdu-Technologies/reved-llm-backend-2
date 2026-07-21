"""Admin — provisioning of teachers, parents, students, classes.

These endpoints are how the platform's institutional data gets seeded.
In production they'd be wrapped in onboarding workflows; here they're
direct one-shot create/link operations.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.session import session_scope
from app.models.parent import Parent
from app.models.school import School, SchoolClass
from app.models.student import Student
from app.models.student_class_membership import StudentClassMembership
from app.models.teacher import Teacher
from app.schemas.admin import (
    ClassRosterRequest,
    ClassRosterResponse,
    ParentSetupRequest,
    ParentSetupResponse,
    TeacherSetupRequest,
    TeacherSetupResponse,
)
from app.services.admin._authz import (
    assert_admin_can_act_on_school,
    resolve_admin_scope,
)
from app.services.cache import invalidate_teacher_progress

logger = logging.getLogger(__name__)


class AdminProvisioningService:
    """Provision teachers / parents / students / classes in one call each."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "AdminProvisioningService":
        return cls()

    async def setup_teacher(
        self,
        request: TeacherSetupRequest,
        *,
        caller_user_id: uuid.UUID | None = None,
        is_dev_stub: bool = False,
    ) -> TeacherSetupResponse:
        # Resolve caller scope BEFORE touching data. Global admins may
        # provision in any school; school-scoped admins are bound to their
        # own school (the target must match, regardless of whether the
        # school already exists or is being created).
        caller_scope = await resolve_admin_scope(
            caller_user_id, is_dev_stub=is_dev_stub
        )
        if caller_scope is None:
            raise NotFoundError("Resource not found.")

        async with session_scope() as session:
            # 1) Find or create the school.
            school = (
                await session.execute(
                    select(School).where(School.name == request.school_name).limit(1)
                )
            ).scalar_one_or_none()
            if school is None:
                # School-scoped admins can only create classes in their
                # own school. They cannot bring a new school into
                # existence — that's a global-admin operation.
                if not caller_scope.is_global:
                    raise NotFoundError("Resource not found.")
                school = School(
                    id=uuid.uuid4(),
                    name=request.school_name,
                    country=request.school_country,
                )
                session.add(school)
                await session.flush()
            else:
                # School-scoped admins can only touch their own school.
                if (
                    not caller_scope.is_global
                    and caller_scope.school_id != school.id
                ):
                    raise NotFoundError("Resource not found.")

            # 2) Find or create the teacher row, linking the supabase user.
            teacher = (
                await session.execute(
                    select(Teacher)
                    .where(Teacher.supabase_user_id == request.supabase_user_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if teacher is None:
                teacher = Teacher(
                    id=uuid.uuid4(),
                    supabase_user_id=request.supabase_user_id,
                    school_id=school.id,
                    full_name=request.full_name,
                    email=request.email,
                    subject_specialty=request.subject_specialty,
                )
                session.add(teacher)
                await session.flush()
            else:
                # Update mutable fields if re-running setup.
                teacher.school_id = school.id
                teacher.full_name = request.full_name
                teacher.email = request.email
                teacher.subject_specialty = request.subject_specialty

            # 3) Create classes (idempotent on name + teacher_id).
            class_ids: list[uuid.UUID] = []
            for spec in request.classes:
                existing = (
                    await session.execute(
                        select(SchoolClass)
                        .where(
                            SchoolClass.school_id == school.id,
                            SchoolClass.teacher_id == teacher.id,
                            SchoolClass.name == spec.name,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    klass = SchoolClass(
                        id=uuid.uuid4(),
                        school_id=school.id,
                        teacher_id=teacher.id,
                        name=spec.name,
                        subject=spec.subject,
                        grade_level=spec.grade_level,
                    )
                    session.add(klass)
                    await session.flush()
                    class_ids.append(klass.id)
                else:
                    # Refresh subject/grade if re-running.
                    existing.subject = spec.subject
                    existing.grade_level = spec.grade_level
                    class_ids.append(existing.id)

        logger.info(
            "Provisioned teacher: school=%s teacher=%s classes=%s",
            school.id, teacher.id, len(class_ids),
        )
        return TeacherSetupResponse(
            school_id=school.id,
            teacher_id=teacher.id,
            class_ids=class_ids,
            linked_user_id=request.supabase_user_id,
            message=(
                f"Teacher '{request.full_name}' provisioned at school "
                f"'{school.name}' with {len(class_ids)} class(es)."
            ),
        )

    async def setup_parent(self, request: ParentSetupRequest) -> ParentSetupResponse:
        async with session_scope() as session:
            # 1) Find or create parent row.
            parent = (
                await session.execute(
                    select(Parent)
                    .where(Parent.supabase_user_id == request.supabase_user_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if parent is None:
                parent = Parent(
                    id=uuid.uuid4(),
                    supabase_user_id=request.supabase_user_id,
                    full_name=request.full_name,
                    email=request.email,
                    phone=request.phone,
                )
                session.add(parent)
                await session.flush()
            else:
                parent.full_name = request.full_name
                parent.email = request.email
                parent.phone = request.phone

            # 2) Create / link student rows.
            student_ids: list[uuid.UUID] = []
            for spec in request.children:
                # Match by supabase_user_id if provided, else by name+parent.
                child: Student | None = None
                if spec.supabase_user_id is not None:
                    child = (
                        await session.execute(
                            select(Student)
                            .where(Student.supabase_user_id == spec.supabase_user_id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if child is None:
                    child = (
                        await session.execute(
                            select(Student)
                            .where(
                                Student.parent_id == parent.id,
                                Student.full_name == spec.full_name,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if child is None:
                    child = Student(
                        id=uuid.uuid4(),
                        supabase_user_id=spec.supabase_user_id,
                        parent_id=parent.id,
                        full_name=spec.full_name,
                        email=spec.email,
                        grade_level=spec.grade_level,
                    )
                    session.add(child)
                    await session.flush()
                else:
                    child.parent_id = parent.id
                    child.full_name = spec.full_name
                    child.email = spec.email
                    child.grade_level = spec.grade_level
                    if spec.supabase_user_id is not None:
                        child.supabase_user_id = spec.supabase_user_id
                student_ids.append(child.id)

        logger.info(
            "Provisioned parent %s with %s child(ren)", parent.id, len(student_ids)
        )
        return ParentSetupResponse(
            parent_id=parent.id,
            linked_user_id=request.supabase_user_id,
            student_ids=student_ids,
            message=(
                f"Parent '{request.full_name}' provisioned with "
                f"{len(student_ids)} linked child(ren)."
            ),
        )

    async def update_class_roster(
        self,
        *,
        class_id: uuid.UUID,
        request: ClassRosterRequest,
        caller_user_id: uuid.UUID | None = None,
        is_dev_stub: bool = False,
    ) -> ClassRosterResponse:
        """Add students to a class. Idempotent on (student_id, class_id) pair.

        The caller's admin scope must include the class's school. Cross-school
        attempts raise ``NotFoundError`` so the response shape matches "no
        such class" and the existence of the class is not leaked.
        """
        from sqlalchemy.exc import IntegrityError

        added: list[uuid.UUID] = []
        skipped_user_ids: list[uuid.UUID] = []

        async with session_scope() as session:
            class_school_id = await session.scalar(
                select(SchoolClass.school_id).where(SchoolClass.id == class_id)
            )
            if class_school_id is None:
                raise NotFoundError(f"Class {class_id} does not exist.")

        # Cross-school check OUTSIDE the session_scope above so the
        # admin-scope query uses its own session. Cleanly composes with
        # the test-fixture monkeypatch on get_sessionmaker.
        await assert_admin_can_act_on_school(
            caller_user_id=caller_user_id,
            target_school_id=class_school_id,
            is_dev_stub=is_dev_stub,
        )

        async with session_scope() as session:
            # Resolve supabase_user_ids → Student.id where possible.
            resolved_student_ids: list[uuid.UUID] = list(request.student_ids)
            for sup_id in request.student_supabase_user_ids:
                row = (
                    await session.execute(
                        select(Student).where(Student.supabase_user_id == sup_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    skipped_user_ids.append(sup_id)
                    continue
                resolved_student_ids.append(row.id)

            for sid in resolved_student_ids:
                # Skip if already enrolled.
                existing = (
                    await session.execute(
                        select(StudentClassMembership).where(
                            StudentClassMembership.student_id == sid,
                            StudentClassMembership.class_id == class_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    added.append(sid)
                    continue
                try:
                    session.add(
                        StudentClassMembership(
                            id=uuid.uuid4(),
                            student_id=sid,
                            class_id=class_id,
                        )
                    )
                    await session.flush()
                    added.append(sid)
                except IntegrityError as exc:
                    # Either dangling FK (no such student) or race — log + skip.
                    logger.warning(
                        "Could not add student %s to class %s: %s", sid, class_id, exc
                    )
                    await session.rollback()

            total = await session.scalar(
                select(func.count(StudentClassMembership.id)).where(
                    StudentClassMembership.class_id == class_id
                )
            )

            # Invalidate the cached class-progress for the teacher of this
            # class so the next dashboard hit reflects the new roster
            # without waiting for the TTL.
            teacher_user_id = await session.scalar(
                select(Teacher.supabase_user_id)
                .join(SchoolClass, SchoolClass.teacher_id == Teacher.id)
                .where(SchoolClass.id == class_id)
            )
        if teacher_user_id is not None:
            await invalidate_teacher_progress(str(teacher_user_id))

        return ClassRosterResponse(
            class_id=class_id,
            added=added,
            skipped_user_ids=skipped_user_ids,
            total_in_class=int(total or 0),
            message=(
                f"{len(added)} student(s) enrolled; "
                f"{len(skipped_user_ids)} skipped (no matching Student row)."
            ),
        )
