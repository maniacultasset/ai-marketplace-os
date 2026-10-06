"""The async engine (psycopg 3; CANON.md:20), built lazily on first use, the session class
with the tenant guard, the tenant transaction boundary and `platform_scope()` (S9; A§6).

- `unit_of_work(tenant_id)` is the one tenant transaction: `session.info` carries the tenant
  and user, the `after_begin` listener re-applies them as transaction-local GUCs on every
  transaction (DATABASE.md:131-132), and a constraint error surfaces as a mapped
  `ConflictError` inside the service, never at teardown (KNOWN-ISSUES.md:50).
- `platform_scope()` is the one cross-tenant bypass: read-only, as `app_platform_ro`, and always
  rolled back (DATABASE.md:154-158). It is the only writer of `session.info[SCOPE_OK]`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Connection, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import get_settings
from app.db.guard import SCOPE_OK, TENANT_INFO_KEY, check_flush, check_scope
from app.db.integrity import to_conflict

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required to build the engine (APP_ROLE api or worker)")
    return create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        pool_recycle=settings.database_pool_recycle,
        # libpq `options`: the psycopg form of a per-connection statement_timeout.
        connect_args={"options": f"-c statement_timeout={settings.db_statement_timeout_ms}"},
    )


async def dispose_engine() -> None:
    """The lifespan shutdown hook. Never builds an engine just to dispose of it."""
    get_sessionmaker.cache_clear()
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()


USER_INFO_KEY = "user_id"


class AppSession(Session):
    """The only session class: the guard checks every ORM statement and every flush."""


event.listen(AppSession, "do_orm_execute", check_scope)
event.listen(AppSession, "before_flush", check_flush)


@event.listens_for(AppSession, "after_begin")
def _apply_transaction_context(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    """Runs first in every transaction the session begins, on its connection."""
    if session.info.get(SCOPE_OK):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql("SET LOCAL ROLE app_platform_ro")
        return
    for key, guc in ((TENANT_INFO_KEY, "app.tenant_id"), (USER_INFO_KEY, "app.user_id")):
        value = session.info.get(key)
        if value is not None:
            connection.execute(select(func.set_config(guc, str(value), True)))


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), sync_session_class=AppSession, expire_on_commit=False)


def _require_uuid(name: str, value: object) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID, got {type(value).__name__}")
    return value


@asynccontextmanager
async def unit_of_work(
    tenant_id: UUID, *, user_id: UUID | None = None
) -> AsyncIterator[AsyncSession]:
    """One tenant transaction. Commits when the block exits cleanly, rolls back otherwise."""
    tenant = _require_uuid("tenant_id", tenant_id)
    user = None if user_id is None else _require_uuid("user_id", user_id)
    async with get_sessionmaker()() as session:
        session.info[TENANT_INFO_KEY] = tenant
        if user is not None:
            session.info[USER_INFO_KEY] = user
        try:
            yield session
            try:
                await session.flush()
                await session.commit()
            except IntegrityError as exc:
                raise to_conflict(exc) from exc
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def platform_scope() -> AsyncIterator[AsyncSession]:
    """The read-only cross-tenant session (A§6 as amended by DATABASE.md:154-158). Every
    transaction runs `SET TRANSACTION READ ONLY` and `SET LOCAL ROLE app_platform_ro` first; the
    session never commits. Importable only from app/admin, app/cli.py and scripts (R5)."""
    async with get_sessionmaker()() as session:
        session.info[SCOPE_OK] = True
        try:
            yield session
        finally:
            await session.rollback()


async def ping(timeout_seconds: float) -> bool:
    """`SELECT 1` within `timeout_seconds`. Any failure is False and logs only the exception's
    class name (driver messages can carry hosts, users or DSN fragments). Cancellation is not
    an `Exception`, so it propagates."""
    try:
        async with asyncio.timeout(timeout_seconds), get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("readiness ping failed: %s", type(exc).__name__)
        return False
    return True
