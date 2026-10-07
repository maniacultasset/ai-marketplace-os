"""The Idempotency-Key service (DATABASE.md:978-981; BACKEND-ARCHITECTURE §5.5; S13 Q3).

`idempotent(principal, key, method, path, payload, handler)` runs `handler(session)` once per
key in one tenant transaction and stores what it returns; a replay (same key, same request
hash, same member) gets the stored `(status, body)` without running it. Another body, another
member or another endpoint under a live key is 409 `IDEMPOTENCY_KEY_REUSED`. The handler returns
only storable responses (2xx, or a 422 it wants replayed); any error it raises rolls the whole
transaction back, the key included, so the client can retry with the same key.

`request_hash` covers `<METHOD> <path>` + NUL + the payload bytes. For a multipart upload the
payload is the `file` part's bytes, not the raw body, whose random boundary would make every
replay a mismatch (Q3).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.repositories.idempotency import Fresh, IdempotencyKeyRepository, Replay
from app.repositories.transactions import unit_of_work

Handler = Callable[[Any], Awaitable[tuple[int, dict[str, Any]]]]
STORED_STATUSES = frozenset({422})


def request_hash(method: str, path: str, payload: bytes) -> bytes:
    return hashlib.sha256(f"{method.upper()} {path}".encode() + b"\x00" + payload).digest()


def key_reused() -> ConflictError:
    return ConflictError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "idempotency_keys")


async def idempotent(
    principal: Principal,
    key: str,
    method: str,
    path: str,
    payload: bytes,
    handler: Handler,
) -> tuple[int, dict[str, Any]]:
    digest = request_hash(method, path, payload)
    endpoint = f"{method.upper()} {path}"
    async with unit_of_work(principal.tenant_id, user_id=principal.user_id) as session:
        keys = IdempotencyKeyRepository(session, principal.tenant_id)
        begun = await keys.begin(key, endpoint, digest, principal.user_id)
        if isinstance(begun, Replay):
            return begun.status, begun.body
        if not isinstance(begun, Fresh):
            raise key_reused()
        status, body = await handler(session)
        if not (200 <= status < 300 or status in STORED_STATUSES):
            raise ValueError(f"an idempotent handler returned {status}; raise errors instead")
        await keys.complete(key, status, body)
    return status, body
