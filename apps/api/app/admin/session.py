"""The admin's only session source (S14b T18): `platform_scope()` behind sqladmin's session-maker
interface. Every admin transaction is read-only as `app_platform_ro` (A§6 as amended,
DATABASE.md:154-158) and never commits; there is no plain-engine fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import platform_scope


class PlatformSessionMaker:
    """What sqladmin calls a session maker: `class_` marks it async, `configure()` takes the
    options sqladmin sets (none apply to a read-only scope), and a call opens `platform_scope()`."""

    class_ = AsyncSession

    def configure(self, **_: Any) -> None:
        """sqladmin passes autoflush/autocommit; the platform scope never writes or commits."""

    def __call__(self, **_: Any) -> AbstractAsyncContextManager[AsyncSession]:
        return _detached_scope()


@asynccontextmanager
async def _detached_scope() -> AsyncIterator[AsyncSession]:
    """`platform_scope()`, with every loaded row detached before its closing rollback: a rollback
    expires the instances, and sqladmin renders them after the session is gone. Only the columns
    a view loaded (`ADMIN_COLUMNS`) stay readable; nothing can lazy-load another."""
    async with platform_scope() as session:
        try:
            yield session
        finally:
            session.expunge_all()
