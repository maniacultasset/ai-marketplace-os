"""The staff tools' platform-admin check (ADR-0019): an email names an active platform admin.

One query on an open connection (the migrator's: `users` is read across every tenant). Shared by
the error-map proposal review (`--by`) and the PTD fetch through a tenant's connection
(`templates refresh --via-tenant`, ADR-0033).
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

ADMIN_SQL: Final = (
    "SELECT id, is_platform_admin, is_active FROM users WHERE lower(email) = lower(:email)"
)


async def active_platform_admin(connection: AsyncConnection, email: str) -> uuid.UUID | None:
    """The user id when `email` is an active platform admin, else None."""
    row = (await connection.execute(text(ADMIN_SQL), {"email": email.strip()})).first()
    if row is None or not row.is_active or not row.is_platform_admin:
        return None
    return uuid.UUID(str(row.id))
