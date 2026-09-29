"""Engine, sessionmaker, dependencies and THE transaction boundary.

Guard contract (one-line change in app/db/guard.py):
    if state.execution_options.get(SCOPE_OK) or state.session.info.get(SCOPE_OK):
        return
platform_scope() sets the session-level flag and is the ONLY sanctioned bypass
of the tenant guard - import-linter permits it from app/admin/** and scripts/**
only. Without it SQLAdmin and the seed script raise CrossTenantQueryError.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession, async_sessionmaker,
                                   create_async_engine)

from app.core.config import settings
from app.core.security import CurrentUser, get_current_user
from app.db.guard import SCOPE_OK


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        pool_recycle=settings.database_pool_recycle,
        pool_pre_ping=True,
        connect_args={"timeout": 10, "server_settings": {
            "application_name": f"{settings.app_name}:{settings.otel_service_name}",
            "statement_timeout": str(settings.db_statement_timeout_ms), "jit": "off"}},
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


def SessionLocal(**kw: Any) -> AsyncSession:  # noqa: N802 - callers write SessionLocal()
    """Lazy: importing app.db never requires a reachable database."""
    return get_sessionmaker()(**kw)


@asynccontextmanager
async def unit_of_work(tenant_id: UUID | None = None) -> AsyncIterator[AsyncSession]:
    """One commit per call, rollback on any exception. Services and workers use
    this. Enqueue ARQ jobs AFTER the block exits, never inside it, or the worker
    dequeues before the commit lands and reads a row that does not exist."""
    async with SessionLocal() as session, session.begin():
        if tenant_id is not None:
            # SET LOCAL, never SET: the GUC must die with the transaction, or a
            # pooled connection carries tenant A's id into tenant B's request.
            await session.execute(text("SET LOCAL app.tenant_id = :tid"),
                                  {"tid": str(tenant_id)})
        yield session


async def tenant_session(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> AsyncIterator[AsyncSession]:
    """Request-scoped session. tenant_id comes from the verified JWT claim only -
    never a path param, query param or body field."""
    async with unit_of_work(user.tenant_id) as session:
        yield session


@asynccontextmanager
async def platform_scope(*, readonly: bool = True) -> AsyncIterator[AsyncSession]:
    """Cross-tenant session for the internal admin and scripts/. Read-only by
    default; readonly=False only for seeding and DLQ requeue on staging."""
    async with SessionLocal() as session:
        session.info[SCOPE_OK] = True
        async with session.begin():
            if readonly:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            yield session


async def dispose_engine() -> None:
    """FastAPI lifespan shutdown and the worker's on_shutdown hook."""
    await get_engine().dispose()
