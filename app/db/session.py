"""Async SQLAlchemy engine and session helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the cached async SQLAlchemy engine.

    Important: Supabase exposes Postgres through Supavisor (the transaction-
    mode pooler) on port 6543. That pooler doesn't preserve prepared
    statements across connections in a way asyncpg expects, so asyncpg's
    own prepared-statement cache fires
    ``DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_1__"
    already exists`` after a few queries.

    Fix: disable asyncpg's prepared-statement cache and pre-cache via
    ``statement_cache_size=0`` and ``prepared_statement_cache_size=0``. With
    these, asyncpg sends every statement as a one-shot query, which the
    transaction pooler handles correctly. The trade-off is a small per-query
    overhead — fine for our load (audit-log writes), much better than
    falling back to direct port 5432 which exhausts Postgres connection
    slots under concurrency.
    """

    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        future=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the cached session factory."""

    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a transactional AsyncSession."""

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional AsyncSession for non-HTTP callers."""

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the cached engine (call on application shutdown)."""

    engine = get_engine()
    await engine.dispose()
