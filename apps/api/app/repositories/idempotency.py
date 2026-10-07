"""`idempotency_keys` (DATABASE.md:978-981; BACKEND-ARCHITECTURE §5.5): the HTTP Idempotency-Key
store, per tenant. A key lives 24 h; an expired key counts as new.

Protocol, in the request's one transaction: `begin()` (INSERT ... ON CONFLICT; a concurrent
first writer's uncommitted row makes this wait until it commits or rolls back), then the job
rows, then `complete()` with the response, then COMMIT. A replay with the same hash and member
gets the stored response; anything else is a mismatch (409). The hourly prune deletes expired
keys tenant by tenant. `request_hash` is never read back to the API, only compared here.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import delete, func, text

from app.db.models.ops import IdempotencyKey
from app.repositories.base import TenantScopedRepository

KEY_TTL_HOURS: Final = 24

_BEGIN: Final = text(
    "INSERT INTO idempotency_keys (tenant_id, key, endpoint, request_hash, user_id, expires_at) "
    "VALUES (:t, :k, :e, :h, :u, now() + make_interval(hours => :ttl)) "
    "ON CONFLICT (tenant_id, key) DO UPDATE SET endpoint = EXCLUDED.endpoint, "
    "request_hash = EXCLUDED.request_hash, user_id = EXCLUDED.user_id, response_status = NULL, "
    "response_json = NULL, expires_at = EXCLUDED.expires_at, created_at = now() "
    "WHERE idempotency_keys.expires_at < now() "
    "RETURNING key"
)
_READ: Final = text(
    "SELECT endpoint, request_hash, user_id, response_status, response_json "
    "FROM idempotency_keys WHERE tenant_id = :t AND key = :k"
)
_PEEK: Final = text(
    "SELECT endpoint, request_hash, user_id, response_status, response_json "
    "FROM idempotency_keys WHERE tenant_id = :t AND key = :k AND expires_at >= now() "
    "AND response_status IS NOT NULL"
)
_COMPLETE: Final = text(
    "UPDATE idempotency_keys SET response_status = :s, response_json = CAST(:b AS jsonb) "
    "WHERE tenant_id = :t AND key = :k"
)


@dataclass(frozen=True, slots=True)
class Fresh:
    """The key is new (or expired): run the handler, then `complete()`."""


@dataclass(frozen=True, slots=True)
class Replay:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Mismatch:
    """The key was used with another request, by another member, or on another endpoint."""


Begun = Fresh | Replay | Mismatch


def _judge(row: Any, endpoint: str, request_hash: bytes, user_id: uuid.UUID) -> Replay | Mismatch:
    same = (
        row.endpoint == endpoint
        and bytes(row.request_hash) == request_hash
        and row.user_id == user_id
        and row.response_status is not None
    )
    return Replay(int(row.response_status), dict(row.response_json or {})) if same else Mismatch()


class IdempotencyKeyRepository(TenantScopedRepository[IdempotencyKey]):
    model = IdempotencyKey

    def _check(self, request_hash: bytes) -> None:
        if not isinstance(request_hash, bytes) or len(request_hash) != 32:
            raise ValueError("request_hash must be 32 bytes (sha256)")

    async def begin(
        self, key: str, endpoint: str, request_hash: bytes, user_id: uuid.UUID
    ) -> Begun:
        self._check(request_hash)
        params = {
            "t": self.tenant_id,
            "k": key,
            "e": endpoint,
            "h": request_hash,
            "u": user_id,
            "ttl": KEY_TTL_HOURS,
        }
        inserted = (await self.db.execute(_BEGIN, params)).scalar_one_or_none()
        if inserted is not None:
            return Fresh()
        row = (await self.db.execute(_READ, {"t": self.tenant_id, "k": key})).one()
        return _judge(row, endpoint, request_hash, user_id)

    async def peek(
        self, key: str, endpoint: str, request_hash: bytes, user_id: uuid.UUID
    ) -> Replay | Mismatch | None:
        """An unlocked read of a live, completed key (a pre-check before slow work)."""
        self._check(request_hash)
        row = (await self.db.execute(_PEEK, {"t": self.tenant_id, "k": key})).one_or_none()
        return None if row is None else _judge(row, endpoint, request_hash, user_id)

    async def complete(self, key: str, status: int, body: dict[str, Any]) -> None:
        result = await self.db.execute(
            _COMPLETE, {"t": self.tenant_id, "k": key, "s": status, "b": json.dumps(body)}
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise RuntimeError("complete() found no begun idempotency key")

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
