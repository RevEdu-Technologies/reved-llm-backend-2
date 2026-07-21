"""Cross-school authorization helpers for admin operations.

Admin endpoints currently sit behind ``require_role("admin")`` which proves
the caller has the *admin* privilege but says nothing about *which school*
they administer. Without a per-school scope check, an admin from School A
could provision teachers/classes inside School B by simply naming it in
the request body.

These helpers resolve the caller's ``Admin.school_id`` at request time
and let services compare it against the operation's target. Mismatches
raise ``NotFoundError`` (→ 404) so the response shape is identical to
"that school/class doesn't exist", avoiding a probing oracle.

Policy notes
------------
* Admins with ``scope='global'`` bypass the school check. ``scope`` lives
  on the ``Admin`` row; today its only value in production is the default
  ``'school'``.
* An authenticated admin user without an ``Admin`` row in the DB is
  treated as "no scope" and denied all school-bound operations. The
  expectation is that admins are provisioned through a separate
  out-of-band flow before they receive admin credentials.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.core.errors import NotFoundError
from app.db.session import session_scope
from app.models.admin import Admin

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminScope:
    """Resolved scope for a calling admin."""

    school_id: uuid.UUID | None
    scope: str  # 'school' or 'global'

    @property
    def is_global(self) -> bool:
        return self.scope == "global"


_DEV_STUB_SCOPE = AdminScope(school_id=None, scope="global")


async def resolve_admin_scope(
    caller_user_id: uuid.UUID | None,
    *,
    is_dev_stub: bool = False,
) -> AdminScope | None:
    """Return the calling admin's scope, or ``None`` if no Admin row exists.

    Callers should refuse the operation when this returns ``None`` — an
    admin token with no provisioned Admin row is a misconfiguration, not
    a free pass.

    Dev-mode bypass: when ``is_dev_stub=True`` (only true when
    ``AUTH_ENABLED=false`` and the caller is the X-Dev-Role stub user),
    return a synthetic global scope so local development can exercise
    admin flows without a seeded Admin row. The production prod-mode
    guard prevents this branch from ever firing in a non-dev environment.
    """

    if is_dev_stub:
        return _DEV_STUB_SCOPE

    if caller_user_id is None:
        return None

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Admin.school_id, Admin.scope).where(
                    Admin.supabase_user_id == caller_user_id
                )
            )
        ).first()

    if row is None:
        return None
    return AdminScope(school_id=row.school_id, scope=row.scope or "school")


async def assert_admin_can_act_on_school(
    *,
    caller_user_id: uuid.UUID | None,
    target_school_id: uuid.UUID,
    is_dev_stub: bool = False,
) -> None:
    """Verify the caller may operate on resources inside ``target_school_id``.

    Raises ``NotFoundError`` on any mismatch — including an admin with no
    Admin row, an admin with a NULL ``school_id``, or a cross-school
    attempt. Global-scope admins always pass.
    """

    scope = await resolve_admin_scope(caller_user_id, is_dev_stub=is_dev_stub)
    if scope is None:
        logger.warning(
            "Admin op denied: no Admin row for caller %s", caller_user_id
        )
        raise NotFoundError("Resource not found.")

    if scope.is_global:
        return

    if scope.school_id is None:
        logger.warning(
            "Admin op denied: caller %s has school-scope but NULL school_id",
            caller_user_id,
        )
        raise NotFoundError("Resource not found.")

    if scope.school_id != target_school_id:
        logger.warning(
            "Admin op denied: caller %s (school=%s) targeted school=%s",
            caller_user_id,
            scope.school_id,
            target_school_id,
        )
        raise NotFoundError("Resource not found.")


__all__ = [
    "AdminScope",
    "assert_admin_can_act_on_school",
    "resolve_admin_scope",
]
