"""The admin's only session source (S14b T18): `platform_scope()` behind sqladmin's session-maker
interface. Every admin transaction is read-only as `app_platform_ro` (A§6 as amended,
DATABASE.md:154-158) and never commits; there is no plain-engine fallback."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
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
        return platform_scope()
