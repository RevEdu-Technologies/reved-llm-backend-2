"""Shared pytest fixtures for the RevEd backend test suite.

Three buckets of fixtures live here:

1. **Lightweight, no I/O** — `auth_headers`, `make_jwt`, `stub_user`, etc.
   Use these for unit tests that don't need a database.

2. **Service mocks** — `mock_groq`, `mock_pinecone`, etc. Patch the heavy
   external clients so tests run fast and deterministically.

3. **DB-backed** (marked with ``@pytest.mark.db``) — `db_engine`,
   `db_session`, `app`, `async_client`, plus async factories like
   `make_school`. These hit a real Postgres (set ``TEST_DATABASE_URL`` to
   point at a throwaway DB; falls back to ``DATABASE_URL``). Each test
   runs inside a transaction that is rolled back at teardown, so DB state
   never leaks between tests.

Async tests just use ``async def`` — ``asyncio_mode = auto`` in
``pytest.ini`` activates pytest-asyncio automatically. Sync tests work
unchanged.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator, Callable

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Load .env before any other code reads environment variables, so DB-backed
# tests can find DATABASE_URL/TEST_DATABASE_URL without depending on shell
# state. Mirrors the loader in ``app.core.config``.
try:
    from dotenv import load_dotenv as _load_dotenv
    from pathlib import Path as _Path

    _load_dotenv(_Path(__file__).resolve().parents[1] / ".env", override=False)
except ImportError:
    pass

# Default the OTEL exporter to ``none`` for tests so the console exporter
# doesn't spam stdout for every span. Individual tests that want to
# observe spans set up their own in-memory exporter via the
# ``InMemorySpanExporter`` pattern (see tests/unit/test_tracing.py).
os.environ.setdefault("OTEL_EXPORTER", "none")
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


# ---------------------------------------------------------------------------
# Section 1 — lightweight fixtures (no I/O)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_user_id() -> uuid.UUID:
    """The dev-mode stub user UUID baked into ``app.core.security``."""

    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def jwt_secret() -> str:
    """Stable secret used by ``make_jwt`` and the test settings overrides."""

    return "test-secret-do-not-use-in-prod"


@pytest.fixture
def make_jwt(jwt_secret: str) -> Callable[..., str]:
    """Build a signed Supabase-style JWT for tests.

    Usage:
        token = make_jwt(role="teacher")              # random user_id
        token = make_jwt(user_id=uid, role="parent")  # specific user
    """

    def _make(
        *,
        user_id: uuid.UUID | None = None,
        role: str = "student",
        email: str | None = "user@test.local",
        audience: str = "authenticated",
        expires_in_seconds: int = 3600,
        algorithm: str = "HS256",
        secret: str | None = None,
    ) -> str:
        claims = {
            "sub": str(user_id or uuid.uuid4()),
            "aud": audience,
            "exp": datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds),
            "iat": datetime.now(tz=timezone.utc),
            "email": email,
            "app_metadata": {"role": role},
        }
        return jwt.encode(claims, secret or jwt_secret, algorithm=algorithm)

    return _make


@pytest.fixture
def auth_headers(make_jwt: Callable[..., str]) -> Callable[..., dict[str, str]]:
    """Build request headers in either dev (``X-Dev-Role``) or prod (``Bearer``) mode.

    Examples:
        auth_headers(role="student")                              # dev mode
        auth_headers(role="teacher", mode="bearer", user_id=uid)  # prod mode
    """

    def _make(
        *,
        role: str = "student",
        mode: str = "dev",
        user_id: uuid.UUID | None = None,
    ) -> dict[str, str]:
        if mode == "dev":
            return {"X-Dev-Role": role}
        if mode == "bearer":
            token = make_jwt(role=role, user_id=user_id)
            return {"Authorization": f"Bearer {token}"}
        raise ValueError(f"Unknown auth mode: {mode!r}")

    return _make


@pytest.fixture
def make_authenticated_user(stub_user_id: uuid.UUID):
    """Construct an ``AuthenticatedUser`` for use in dependency overrides."""

    from app.core.security import AuthenticatedUser

    def _make(
        *,
        role: str = "student",
        user_id: uuid.UUID | None = None,
        email: str | None = "user@test.local",
        is_stub: bool = False,
    ) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=user_id or stub_user_id,
            email=email,
            role=role,  # type: ignore[arg-type]
            is_stub=is_stub,
        )

    return _make


# ---------------------------------------------------------------------------
# Section 2 — service mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_groq(monkeypatch: pytest.MonkeyPatch):
    """Patch ``GroqLLMClient.generate`` so tests never hit the real API.

    Returns a list of recorded prompts so tests can assert on what was sent.
    Override the canned response per-test by assigning to ``.response``.
    """

    recorded: list[dict[str, str]] = []

    class _FakeGroqResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    class _MockGroq:
        response: str = "Test answer from mocked Groq client."

        def generate(self, *, system_prompt: str, user_prompt: str, **kwargs):
            recorded.append({"system": system_prompt, "user": user_prompt})
            return _FakeGroqResponse(self.response)

    mock = _MockGroq()

    # Patch every known import site. The client is constructed inside
    # service factories; intercepting the class constructor catches them all.
    try:
        from app.llm import groq_client as _groq_mod

        monkeypatch.setattr(_groq_mod, "GroqLLMClient", lambda *a, **k: mock)
    except ImportError:
        pass

    mock.calls = recorded  # type: ignore[attr-defined]
    return mock


@pytest.fixture
def mock_pinecone(monkeypatch: pytest.MonkeyPatch):
    """Stub the Pinecone retriever to return one canned RetrievalResult."""

    try:
        from app.rag.retrieval.retriever import RetrievalResult
    except ImportError:
        pytest.skip("Pinecone retriever module not importable in this environment.")

    canned = [
        RetrievalResult(
            score=0.92,
            chunk_id="chunk-test-1",
            document_id="doc-test-1",
            source_file="test-source.txt",
            subject="physics",
            content_type="text",
            chunk_index=0,
            text="Newton's first law: an object at rest stays at rest unless acted on by a force.",
        )
    ]

    class _MockRetriever:
        results = canned

        def retrieve(self, query_text, *, top_k=5, subject=None, namespace=None):
            return self.results

    mock = _MockRetriever()

    try:
        from app.rag.retrieval import retriever as _ret_mod

        monkeypatch.setattr(_ret_mod, "PineconeRetriever", lambda *a, **k: mock)
    except (ImportError, AttributeError):
        pass

    return mock


# ---------------------------------------------------------------------------
# Section 3 — DB-backed fixtures
# ---------------------------------------------------------------------------


def _resolve_test_db_url() -> str | None:
    """Pick the test DB URL: prefer TEST_DATABASE_URL, fall back to DATABASE_URL."""

    raw = os.getenv("TEST_DATABASE_URL", "").strip() or os.getenv(
        "DATABASE_URL", ""
    ).strip()
    if not raw:
        return None
    # Normalize to async driver (matches app.core.config._as_async_db_url).
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://") :]
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw[len("postgres://") :]
    return raw


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Async engine for the current test, using NullPool so every connection is fresh.

    NullPool is important: without it, asyncpg connections get cached on
    one event loop and reused on another, blowing up with
    "Future attached to a different loop" errors. NullPool means each
    ``engine.connect()`` opens a brand-new connection on the current
    loop — slightly slower per-test, but trivially correct.
    """

    url = _resolve_test_db_url()
    if not url:
        pytest.skip("No TEST_DATABASE_URL/DATABASE_URL configured; skipping DB tests.")
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession wrapped in a transaction that is rolled back at teardown.

    Any ``session.commit()`` inside the test becomes a savepoint release; the
    outer transaction is unconditionally rolled back, so no rows persist.

    Critically, this fixture also monkeypatches the app's
    ``get_sessionmaker`` / ``get_engine`` to point at the test connection.
    Without that, services that use ``session_scope()`` open their own
    connections against the production engine and never see the rows the
    test factories created — defeating the whole transactional setup.
    """

    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        test_sessionmaker = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )

        # Redirect every code path that goes through the app's session
        # helpers to our transactional connection. Importing here avoids
        # a circular dependency at module-load time.
        from app.db import session as _session_module

        # Clear the session module's own lru_caches BEFORE patching, so
        # that any prior test (or app boot path) that populated the cache
        # with a real-engine sessionmaker — or a stale test-engine one
        # bound to a now-closed connection — can't leak into this test
        # via a captured ``from app.db.session import get_sessionmaker``
        # reference. The patch below then ensures fresh calls return the
        # test sessionmaker; the cache_clear ensures stale entries never
        # do.
        _session_module.get_sessionmaker.cache_clear()
        _session_module.get_engine.cache_clear()

        monkeypatch.setattr(_session_module, "get_sessionmaker", lambda: test_sessionmaker)
        monkeypatch.setattr(_session_module, "get_engine", lambda: db_engine)

        # Clear every cached service singleton in
        # ``app.api.dependencies``. Service constructors capture the
        # sessionmaker once at __init__, so a service cached from a
        # previous test would still point at the previous test's
        # (now-closed) connection. Forcing a fresh service per test
        # ensures every repository picks up the monkeypatched sessionmaker.
        from app.api import dependencies as _deps_module

        for _name in dir(_deps_module):
            _obj = getattr(_deps_module, _name)
            if hasattr(_obj, "cache_clear"):
                _obj.cache_clear()

        async with test_sessionmaker() as session:
            try:
                yield session
            finally:
                await session.close()
        await transaction.rollback()


# ---------------------------------------------------------------------------
# Section 4 — app + client fixtures (DB-backed)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_factory(db_session: AsyncSession, make_authenticated_user):
    """Return a callable that builds a FastAPI app with DI overrides applied.

    Usage:
        app = app_factory(role="teacher", user_id=teacher.supabase_user_id)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            ...

    The DB dependency is overridden to use the test session, so any
    ``session.commit()`` your route runs is part of the rolled-back outer
    transaction.
    """

    from app.core.security import AuthenticatedUser, get_current_user
    from app.db.session import get_db_session
    from main import app as fastapi_app

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    def _factory(
        *,
        role: str = "student",
        user_id: uuid.UUID | None = None,
        email: str | None = "user@test.local",
    ):
        user: AuthenticatedUser = make_authenticated_user(
            role=role, user_id=user_id, email=email, is_stub=False
        )

        async def _override_user() -> AuthenticatedUser:
            return user

        fastapi_app.dependency_overrides[get_db_session] = _override_db
        fastapi_app.dependency_overrides[get_current_user] = _override_user
        return fastapi_app

    yield _factory

    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def async_client(app_factory) -> AsyncIterator[Callable[..., AsyncClient]]:
    """Build an httpx.AsyncClient bound to an app configured for a given user.

    Usage:
        async with async_client(role="teacher", user_id=t.supabase_user_id) as c:
            r = await c.get("/api/v1/teacher/generations")
    """

    clients: list[AsyncClient] = []

    def _open(**kwargs) -> AsyncClient:
        app = app_factory(**kwargs)
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        )
        clients.append(client)
        return client

    yield _open

    for c in clients:
        await c.aclose()


# ---------------------------------------------------------------------------
# Section 5 — data factories
# ---------------------------------------------------------------------------
#
# Factories are async helpers that persist rows and return them. They take
# the test ``db_session`` directly. Use defaults for fields you don't care
# about; override only what the test asserts on.


@pytest_asyncio.fixture
async def make_school(db_session: AsyncSession):
    from app.models.school import School

    async def _make(*, name: str = "Test School", **overrides) -> School:
        row = School(name=name, **overrides)
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_teacher(db_session: AsyncSession):
    from app.models.teacher import Teacher

    async def _make(
        *,
        school_id: uuid.UUID | None = None,
        full_name: str = "Test Teacher",
        supabase_user_id: uuid.UUID | None = None,
        email: str | None = None,
        subject_specialty: str | None = None,
        **overrides,
    ) -> Teacher:
        row = Teacher(
            school_id=school_id,
            full_name=full_name,
            supabase_user_id=supabase_user_id or uuid.uuid4(),
            email=email,
            subject_specialty=subject_specialty,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_student(db_session: AsyncSession):
    from app.models.student import Student

    async def _make(
        *,
        school_id: uuid.UUID | None = None,
        parent_id: uuid.UUID | None = None,
        full_name: str = "Test Student",
        supabase_user_id: uuid.UUID | None = None,
        grade_level: str | None = None,
        **overrides,
    ) -> Student:
        row = Student(
            school_id=school_id,
            parent_id=parent_id,
            full_name=full_name,
            supabase_user_id=supabase_user_id or uuid.uuid4(),
            grade_level=grade_level,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_parent(db_session: AsyncSession):
    from app.models.parent import Parent

    async def _make(
        *,
        full_name: str = "Test Parent",
        supabase_user_id: uuid.UUID | None = None,
        email: str | None = None,
        phone: str | None = None,
        **overrides,
    ) -> Parent:
        row = Parent(
            full_name=full_name,
            supabase_user_id=supabase_user_id or uuid.uuid4(),
            email=email,
            phone=phone,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_admin(db_session: AsyncSession):
    from app.models.admin import Admin

    async def _make(
        *,
        school_id: uuid.UUID | None = None,
        full_name: str = "Test Admin",
        supabase_user_id: uuid.UUID | None = None,
        scope: str = "school",
        **overrides,
    ) -> Admin:
        row = Admin(
            school_id=school_id,
            full_name=full_name,
            supabase_user_id=supabase_user_id or uuid.uuid4(),
            scope=scope,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_class(db_session: AsyncSession):
    from app.models.school import SchoolClass

    async def _make(
        *,
        school_id: uuid.UUID,
        teacher_id: uuid.UUID | None = None,
        name: str = "Test Class",
        grade_level: str | None = None,
        subject: str | None = None,
        **overrides,
    ) -> SchoolClass:
        row = SchoolClass(
            school_id=school_id,
            teacher_id=teacher_id,
            name=name,
            grade_level=grade_level,
            subject=subject,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_membership(db_session: AsyncSession):
    from app.models.student_class_membership import StudentClassMembership

    async def _make(
        *,
        student_id: uuid.UUID,
        class_id: uuid.UUID,
        **overrides,
    ) -> StudentClassMembership:
        row = StudentClassMembership(
            student_id=student_id, class_id=class_id, **overrides
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_goal(db_session: AsyncSession):
    from app.models.student import Goal

    async def _make(
        *,
        student_id: uuid.UUID,
        title: str = "Test goal",
        subject: str | None = None,
        target_date: date | None = None,
        **overrides,
    ) -> Goal:
        row = Goal(
            student_id=student_id,
            title=title,
            subject=subject,
            target_date=target_date,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_ai_generation(db_session: AsyncSession):
    from app.models.ai_generation import AIGeneration

    async def _make(
        *,
        user_id: uuid.UUID | None,
        role: str,
        generation_type: str,
        title: str = "Test generation",
        request_payload: dict | None = None,
        response_payload: dict | None = None,
        **overrides,
    ) -> AIGeneration:
        row = AIGeneration(
            user_id=user_id,
            role=role,
            generation_type=generation_type,
            title=title,
            request_payload=request_payload or {},
            response_payload=response_payload or {},
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


@pytest_asyncio.fixture
async def make_notification(db_session: AsyncSession):
    from app.models.notification import Notification

    async def _make(
        *,
        recipient_user_id: uuid.UUID,
        recipient_role: str,
        category: str = "info",
        title: str = "Test notification",
        body: str = "Test body",
        **overrides,
    ) -> Notification:
        row = Notification(
            recipient_user_id=recipient_user_id,
            recipient_role=recipient_role,
            category=category,
            title=title,
            body=body,
            **overrides,
        )
        db_session.add(row)
        await db_session.flush()
        return row

    return _make


# ---------------------------------------------------------------------------
# Section 6 — composite fixtures (common test scenarios)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_schools(make_school, make_teacher, make_student, make_parent):
    """A pre-built two-school scenario for cross-tenant negative tests.

    Returns a dict:
        {
            "school_a": School,
            "school_b": School,
            "teacher_a": Teacher (in school_a),
            "teacher_b": Teacher (in school_b),
            "student_a": Student (in school_a),
            "student_b": Student (in school_b),
            "parent_a": Parent (linked to student_a),
            "parent_b": Parent (linked to student_b),
        }
    """

    school_a = await make_school(name="School A")
    school_b = await make_school(name="School B")
    teacher_a = await make_teacher(school_id=school_a.id, full_name="Teacher A")
    teacher_b = await make_teacher(school_id=school_b.id, full_name="Teacher B")
    parent_a = await make_parent(full_name="Parent A")
    parent_b = await make_parent(full_name="Parent B")
    student_a = await make_student(
        school_id=school_a.id, parent_id=parent_a.id, full_name="Student A"
    )
    student_b = await make_student(
        school_id=school_b.id, parent_id=parent_b.id, full_name="Student B"
    )
    return {
        "school_a": school_a,
        "school_b": school_b,
        "teacher_a": teacher_a,
        "teacher_b": teacher_b,
        "student_a": student_a,
        "student_b": student_b,
        "parent_a": parent_a,
        "parent_b": parent_b,
    }
