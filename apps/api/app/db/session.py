"""The async engine (psycopg 3; CANON.md:20), built lazily on first use, the session class
with the tenant guard, the tenant transaction boundary and `platform_scope()` (S9; A§6).

- `unit_of_work(tenant_id)` is the one tenant transaction: `session.info` carries the tenant
  and user, the `after_begin` listener re-applies them as transaction-local GUCs on every
  transaction (DATABASE.md:131-132), and a constraint error surfaces as a mapped
  `ConflictError` inside the service, never at teardown (KNOWN-ISSUES.md:50).
- `platform_scope()` is the one cross-tenant bypass: read-only, as `app_platform_ro`, and always
  rolled back (DATABASE.md:154-158). It is the only writer of `session.info[SCOPE_OK]`.
- `identity_session()` is the transaction of the public auth flows (login, refresh, logout), which
  start with no tenant: `set_gucs()` sets `app.login_email`, `app.token_hash`, `app.user_id` and
  `app.tenant_id` as the flow learns them (DATABASE.md:306-312), each in `session.info` too, so the
  `after_begin` listener re-applies them on the next transaction.
- `identity_scope(stmt)` stamps `SCOPE_OK` on one identity statement only (the BACKEND-ARCHITECTURE
  §9 named exception: login, refresh and switch read the caller's own memberships and tokens across
  tenants, which RLS narrows to the caller's rows). It never touches `session.info`, so the session
  stays read-write as `app_rw`. Only `app.repositories.identity` may import it (repo rule R7).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Executable, event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import get_settings
from app.db.guard import SCOPE_OK, TENANT_INFO_KEY, check_flush, check_scope
from app.db.integrity import map_db_error, to_conflict

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required to build the engine (APP_ROLE api or worker)")
    return create_async_engine(
        settings.database_url,
        # Bound values (a hashed_password INSERT) never reach a log line. No `echo=`: SQLAlchemy would
        # attach its own plain-text stdout handler; configure_logging owns the level (S11 C5).
        hide_parameters=True,
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
LOGIN_EMAIL_INFO_KEY = "login_email"
TOKEN_HASH_INFO_KEY = "token_hash"  # noqa: S105 - a session.info key
# session.info key -> the transaction-local GUC the listener re-applies (DATABASE.md:131-132).
_GUC_KEYS: tuple[tuple[str, str], ...] = (
    (TENANT_INFO_KEY, "app.tenant_id"),
    (USER_INFO_KEY, "app.user_id"),
    (LOGIN_EMAIL_INFO_KEY, "app.login_email"),
    (TOKEN_HASH_INFO_KEY, "app.token_hash"),
)


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
    for key, guc in _GUC_KEYS:
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
        except BaseException as exc:
            await session.rollback()
            # a 0004 guard's refusal (in the block, or deferred to the commit) as its DomainError
            mapped = map_db_error(exc) if isinstance(exc, DBAPIError) else None
            if mapped is not None:
                raise mapped from exc
            raise


async def _end(session: AsyncSession) -> None:
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        raise to_conflict(exc) from exc


async def _rollback_and_map(session: AsyncSession, exc: BaseException) -> None:
    await session.rollback()
    mapped = map_db_error(exc) if isinstance(exc, DBAPIError) else None
    if mapped is not None:
        raise mapped from exc


@asynccontextmanager
async def identity_session() -> AsyncIterator[AsyncSession]:
    """The public auth flows' transaction: no tenant yet (`unit_of_work` needs one). Commits when
    the block exits cleanly, rolls back otherwise; a guard refusal surfaces as its DomainError."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await _end(session)
        except BaseException as exc:
            await _rollback_and_map(session, exc)
            raise


async def set_gucs(
    session: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    user_id: UUID | None = None,
    login_email: str | None = None,
    token_hash: bytes | None = None,
) -> None:
    """Set the given GUCs now (transaction-local) and for every later transaction of `session`.
    `login_email` is lower-cased (the users_login policy compares `lower(email)`); `token_hash`
    is the raw sha256, sent as hex (`app_token_hash()` decodes it). There is deliberately no
    `job_id`: only the worker names a job (CODE-PLAN S10 L10)."""
    values: list[tuple[str, str]] = []
    if tenant_id is not None:
        session.info[TENANT_INFO_KEY] = _require_uuid("tenant_id", tenant_id)
        values.append(("app.tenant_id", str(tenant_id)))
    if user_id is not None:
        session.info[USER_INFO_KEY] = _require_uuid("user_id", user_id)
        values.append(("app.user_id", str(user_id)))
    if login_email is not None:
        email = login_email.strip().lower()
        if not email:
            raise ValueError("login_email must not be blank")
        session.info[LOGIN_EMAIL_INFO_KEY] = email
        values.append(("app.login_email", email))
    if token_hash is not None:
        if not isinstance(token_hash, bytes) or len(token_hash) != 32:
            raise ValueError("token_hash must be 32 bytes (sha256)")
        session.info[TOKEN_HASH_INFO_KEY] = token_hash.hex()
        values.append(("app.token_hash", token_hash.hex()))
    for guc, value in values:
        await session.execute(select(func.set_config(guc, value, True)))


def identity_scope[StmtT: Executable](stmt: StmtT) -> StmtT:
    """One identity statement past the tenant guard (per statement, never `session.info`)."""
    stamped: Any = stmt.execution_options(**{SCOPE_OK: True})
    return stamped  # type: ignore[no-any-return]


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
