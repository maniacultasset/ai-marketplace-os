"""`idempotency_keys` (DATABASE.md:978-981; BACKEND-ARCHITECTURE §5.5): the HTTP Idempotency-Key
store, per tenant. Keys live 24 h; the hourly periodic prune deletes expired ones per tenant."""

from __future__ import annotations

from sqlalchemy import delete, func

from app.db.models.ops import IdempotencyKey
from app.repositories.base import TenantScopedRepository


class IdempotencyKeyRepository(TenantScopedRepository[IdempotencyKey]):
    model = IdempotencyKey

    async def prune_expired(self) -> int:
        """Delete this tenant's keys whose `expires_at` has passed; returns the count."""
        stmt = self._scoped_write(
            delete(IdempotencyKey).where(
                self._tenant_criterion(include_platform=False),
                IdempotencyKey.expires_at < func.now(),
            )
        )
        result = await self.db.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)
