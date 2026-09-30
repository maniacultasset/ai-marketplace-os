"""The async engine (psycopg 3; CANON.md:20), built lazily on first use.

S1 holds only the engine, its disposal and the readiness query. S9 extends this file with the
sessionmaker, the transaction boundary and `platform_scope()`.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings

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
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()


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
