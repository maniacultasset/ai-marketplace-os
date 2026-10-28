"""The marketplace account purge (Amazon DPP 1.7; migration 0011b): which accounts are due, and
the migrator call that deletes one of them.

- `due_accounts(session, limit)`: `(tenant_id, id)` of every account `revoked` for at least
  `PURGE_AFTER_DAYS` days, or `needs_reconnect` for that long, oldest first. Cross-tenant: run it
  only in `platform_scope()` (the daily maintenance job). Reads no credential column.
- `purger()`: a migrator engine (ALEMBIC_DATABASE_URL, one connection) for the run. `purge()`
  calls `purge_marketplace_account(t, a)` in one transaction and, before that transaction
  commits, hands the returned submission object keys to `delete_objects`: a failed delete rolls
  the purge back, so no row is gone while its objects stay, and the next run retries.

`PURGE_AFTER_DAYS` is also a literal in the SQL function (`interval '25 days'`); a unit test keeps
the two equal. 25 days leaves a daily job five tries inside DPP 1.7's 30 days.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings
from app.db.models.catalog_assets import MarketplaceAccount

PURGE_AFTER_DAYS: Final = 25
PURGE_AFTER: Final = timedelta(days=PURGE_AFTER_DAYS)
_PURGE: Final = text("SELECT public.purge_marketplace_account(:t, :a)")

DeleteObjects = Callable[[Sequence[str]], Awaitable[None]]


async def due_accounts(session: Any, *, limit: int = 100) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Accounts whose access ended `PURGE_AFTER_DAYS` days ago or more, oldest first."""
    model = MarketplaceAccount
    cutoff = func.now() - PURGE_AFTER
    ended = func.coalesce(model.revoked_at, model.needs_reconnect_since)
    stmt = (
        select(model.tenant_id, model.id)
        .where(
            or_(
                and_(model.status == "revoked", model.revoked_at <= cutoff),
                and_(model.status == "needs_reconnect", model.needs_reconnect_since <= cutoff),
            )
        )
        .order_by(ended, model.id)
        .limit(limit)
    )
    return [(row.tenant_id, row.id) for row in (await session.execute(stmt)).all()]


@dataclass(frozen=True)
class PurgeResult:
    row_counts: dict[str, int]
    objects: int


class Purger:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def purge(
        self, tenant_id: uuid.UUID, account_id: uuid.UUID, delete_objects: DeleteObjects
    ) -> PurgeResult:
        async with self._engine.begin() as connection:
            result = (
                await connection.execute(_PURGE, {"t": tenant_id, "a": account_id})
            ).scalar_one()
            keys = [str(key) for key in result.get("object_keys") or []]
            if keys:
                await delete_objects(keys)
        counts = {str(k): int(v) for k, v in (result.get("row_counts") or {}).items()}
        return PurgeResult(row_counts=counts, objects=len(keys))


@asynccontextmanager
async def purger() -> AsyncIterator[Purger]:
    """A migrator engine for one maintenance run; ALEMBIC_DATABASE_URL must be set."""
    url = get_settings().alembic_database_url
    if url is None:
        raise RuntimeError("the account purge needs ALEMBIC_DATABASE_URL")
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        yield Purger(engine)
    finally:
        await engine.dispose()
