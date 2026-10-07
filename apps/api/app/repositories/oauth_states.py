"""`oauth_states` (DATABASE.md §5.4; S15): the OAuth state rows and the callback's pending rows.

- A *state* row is written by connect: the sha256 of the `state` sent to the marketplace, the
  user who started the flow, the marketplace, the PKCE verifier (Flipkart; NULL for Amazon) and a
  TTL taken from the database clock. The public callback consumes it once by hash
  (`identity.consume_oauth_state`, under the 0007 hash branch).
- A *pending* row is written by the callback in the state's tenant: the sha256 of a fresh opaque
  handle, the sealed token pair, its key version, the seller, region, scopes and the token's
  expiry. The authenticated confirm consumes it once here, under the tenant policy, and only for
  the user who started the flow.

Every statement names this repository's tenant; each consume is one `DELETE ... RETURNING`, so a
row is used at most once even under concurrent requests. Row values are never logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from app.db.models.ops import OAuthState
from app.repositories.base import TenantScopedRepository

if TYPE_CHECKING:
    from app.adapters.oauth import TokenGrant
    from app.core.crypto import SealedTokens

_INSERT_STATE: Final = text(
    "INSERT INTO oauth_states (tenant_id, state_hash, user_id, marketplace, code_verifier, "
    "expires_at) VALUES (:t, :h, :u, :m, :v, now() + make_interval(secs => :ttl))"
)
_INSERT_PENDING: Final = text(
    "INSERT INTO oauth_states (tenant_id, state_hash, user_id, marketplace, expires_at, "
    "token_enc, kms_key_version, seller_id, region, scopes, token_expires_at) "
    "VALUES (:t, :h, :u, :m, now() + make_interval(secs => :ttl), :enc, :kv, :seller, :region, "
    ":scopes, :token_expires_at)"
)
_CONSUME_PENDING: Final = text(
    "DELETE FROM oauth_states WHERE tenant_id = :t AND state_hash = :h AND user_id = :u "
    "AND token_enc IS NOT NULL AND expires_at > now() "
    "RETURNING marketplace, token_enc, kms_key_version, seller_id, region, scopes, "
    "token_expires_at"
)
_PRUNE: Final = text("DELETE FROM oauth_states WHERE tenant_id = :t AND expires_at <= now()")


def _hash(name: str, value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{name} must be 32 bytes (sha256)")
    return value


def _ttl(ttl_s: int) -> int:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not 1 <= ttl_s <= 86_400:
        raise ValueError("ttl_s must be an int in 1..86400")
    return ttl_s


@dataclass(frozen=True, slots=True)
class PendingRow:
    marketplace: str
    token_enc: bytes = field(repr=False)
    kms_key_version: str
    seller_id: str
    region: str | None
    scopes: tuple[str, ...]
    token_expires_at: datetime | None


class OAuthStateRepository(TenantScopedRepository[OAuthState]):
    model = OAuthState

    async def insert_state(
        self,
        *,
        state_hash: bytes,
        user_id: uuid.UUID,
        marketplace: str,
        code_verifier: str | None,
        ttl_s: int,
    ) -> None:
        await self.db.execute(
            _INSERT_STATE,
            {
                "t": self.tenant_id,
                "h": _hash("state_hash", state_hash),
                "u": user_id,
                "m": marketplace,
                "v": code_verifier,
                "ttl": _ttl(ttl_s),
            },
        )

    async def insert_pending(
        self,
        *,
        handle_hash: bytes,
        user_id: uuid.UUID,
        marketplace: str,
        sealed: SealedTokens,
        grant: TokenGrant,
        ttl_s: int,
    ) -> None:
        if grant.seller_id is None:
            raise ValueError("a pending row needs the seller id")
        await self.db.execute(
            _INSERT_PENDING,
            {
                "t": self.tenant_id,
                "h": _hash("handle_hash", handle_hash),
                "u": user_id,
                "m": marketplace,
                "ttl": _ttl(ttl_s),
                "enc": sealed.blob,
                "kv": sealed.key_version,
                "seller": grant.seller_id,
                "region": grant.region,
                "scopes": list(grant.scopes),
                "token_expires_at": grant.expires_at,
            },
        )

    async def consume_pending(self, *, handle_hash: bytes, user_id: uuid.UUID) -> PendingRow | None:
        """The live pending row for this handle and user, deleted; None if there is none."""
        row = (
            await self.db.execute(
                _CONSUME_PENDING,
                {"t": self.tenant_id, "h": _hash("handle_hash", handle_hash), "u": user_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return PendingRow(
            marketplace=row.marketplace,
            token_enc=bytes(row.token_enc),
            kms_key_version=row.kms_key_version,
            seller_id=row.seller_id,
            region=row.region,
            scopes=tuple(row.scopes or ()),
            token_expires_at=row.token_expires_at,
        )

    async def prune_expired(self) -> int:
        """Delete this tenant's expired state and pending rows; the count."""
        result = await self.db.execute(_PRUNE, {"t": self.tenant_id})
        return int(getattr(result, "rowcount", 0) or 0)
