"""`marketplace_accounts` (DATABASE.md:1000-1016; S15): the connected seller accounts.

- One live (non-revoked) account per tenant and marketplace (`uq_marketplace_accounts_tenant_id_
  marketplace`), and one live account per marketplace seller across all tenants (T31:
  `uq_marketplace_accounts_marketplace_seller_id`, beneath RLS). `insert_live` arbitrates the
  first with `ON CONFLICT ... DO NOTHING`; a violation of the second is `SellerLiveElsewhere`,
  which says nothing about the other tenant.
- Revoke deletes the live copy of the tokens: `token_enc` and `kms_key_version` NULL
  (`ck_ma_revoked_shredded`), so a revoked account needs no plaintext.
- The worker's refresh reads a snapshot in one short transaction and writes back with a
  compare-and-set on `status = 'active' AND updated_at = <seen>`, so a revoke or reconnect that
  landed in between always wins. `refresh_lock` is a session-level try-advisory lock on its own
  AUTOCOMMIT connection, so no transaction stays open across the marketplace call
  (DESIGN.md:66).

`token_enc` is selected only by `snapshot` (for the worker's unseal) and never logged.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import exists, func, literal, select, text, tuple_
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError
from app.db.models.catalog_assets import UQ_MA_SELLER_LIVE, MarketplaceAccount
from app.db.models.tenancy import Tenant
from app.db.session import get_engine
from app.repositories.base import TenantScopedRepository

if TYPE_CHECKING:
    from app.core.crypto import SealedTokens

LIVE_TENANT: Final = ("trial", "active")
PAGE_LIMIT_MIN: Final = 1
PAGE_LIMIT_MAX: Final = 200


class SellerLiveElsewhere(ConflictError):  # noqa: N818 - the plan's name for this outcome
    """The seller account is live in another tenant (T31). Carries no row data."""

    def __init__(self) -> None:
        from mkos_schemas.codes import ErrorCode

        super().__init__(ErrorCode.MARKETPLACE_ACCOUNT_CONFLICT, UQ_MA_SELLER_LIVE)


@dataclass(frozen=True, slots=True)
class AccountState:
    """What the adapter wiring needs (S20 T15): no token material."""

    id: uuid.UUID
    marketplace: str
    seller_id: str
    status: str


@dataclass(frozen=True, slots=True)
class RefreshSnapshot:
    id: uuid.UUID
    marketplace: str
    status: str
    token_enc: bytes | None = field(repr=False)
    kms_key_version: str | None
    expires_at: datetime | None
    updated_at: datetime
    due: bool


_INSERT_LIVE: Final = text(
    "INSERT INTO marketplace_accounts (tenant_id, marketplace, seller_id, region, scopes, "
    "token_enc, kms_key_version, expires_at, connected_by_user_id) "
    "VALUES (:t, :m, :seller, :region, :scopes, :enc, :kv, :expires_at, :user) "
    "ON CONFLICT (tenant_id, marketplace) WHERE status <> 'revoked' DO NOTHING RETURNING id"
)
_RECONNECT: Final = text(
    "UPDATE marketplace_accounts SET status = 'active', token_enc = :enc, kms_key_version = :kv, "
    "expires_at = :expires_at, region = :region, scopes = :scopes, connected_by_user_id = :user, "
    "needs_reconnect_since = NULL, last_refresh_error_code = NULL, revoked_at = NULL, "
    "status_changed_at = CASE WHEN status = 'active' THEN status_changed_at ELSE now() END "
    "WHERE tenant_id = :t AND id = :id AND status <> 'revoked'"
)
_REVOKE: Final = text(
    "UPDATE marketplace_accounts SET status = 'revoked', token_enc = NULL, kms_key_version = NULL, "
    "needs_reconnect_since = NULL, revoked_at = now(), status_changed_at = now() "
    "WHERE tenant_id = :t AND id = :id AND status <> 'revoked'"
)
_SNAPSHOT: Final = text(
    "SELECT id, marketplace, status, token_enc, kms_key_version, expires_at, updated_at, "
    "(status = 'active' AND (expires_at IS NULL OR expires_at < now() + :margin)) AS due "
    "FROM marketplace_accounts WHERE tenant_id = :t AND id = :id"
)
_CAS_TOKENS: Final = text(
    "UPDATE marketplace_accounts SET token_enc = :enc, kms_key_version = :kv, "
    "expires_at = :expires_at, last_refresh_at = now(), last_refresh_error_code = NULL "
    "WHERE tenant_id = :t AND id = :id AND status = 'active' AND updated_at = :seen"
)
_CAS_NEEDS_RECONNECT: Final = text(
    "UPDATE marketplace_accounts SET status = 'needs_reconnect', needs_reconnect_since = now(), "
    "last_refresh_error_code = 'AUTH_EXPIRED', status_changed_at = now() "
    "WHERE tenant_id = :t AND id = :id AND status = 'active' AND updated_at = :seen"
)
_CAS_ERROR: Final = text(
    "UPDATE marketplace_accounts SET last_refresh_error_code = :code "
    "WHERE tenant_id = :t AND id = :id AND status = 'active' AND updated_at = :seen"
)
_TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
_UNLOCK: Final = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class MarketplaceAccountRepository(TenantScopedRepository[MarketplaceAccount]):
    model = MarketplaceAccount

    async def page(
        self, *, limit: int, after: tuple[datetime, uuid.UUID] | None
    ) -> tuple[builtins.list[MarketplaceAccount], bool]:
        """Every account of the tenant, every status, newest first (keyset)."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}")
        model = MarketplaceAccount
        stmt = self._select()
        if after is not None:
            created_at, row_id = after
            bound = tuple_(
                literal(created_at, model.created_at.type), literal(row_id, model.id.type)
            )
            stmt = stmt.where(tuple_(model.created_at, model.id) < bound)
        stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit

    async def get_for_update(self, account_id: uuid.UUID) -> MarketplaceAccount | None:
        stmt = self._select().where(MarketplaceAccount.id == account_id).with_for_update()
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def live_for_update(self, marketplace: str) -> MarketplaceAccount | None:
        """The tenant's non-revoked account for `marketplace`, locked."""
        stmt = (
            self._select()
            .where(
                MarketplaceAccount.marketplace == marketplace,
                MarketplaceAccount.status != "revoked",
            )
            .with_for_update()
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def reload(self, account: MarketplaceAccount) -> MarketplaceAccount:
        await self.db.refresh(account)
        return account

    async def insert_live(
        self,
        *,
        marketplace: str,
        seller_id: str,
        region: str | None,
        scopes: tuple[str, ...],
        sealed: SealedTokens,
        expires_at: datetime | None,
        connected_by: uuid.UUID,
    ) -> MarketplaceAccount | None:
        """The new live account; None when the tenant already has one for `marketplace`
        (lost a race). The seller live in any other tenant is `SellerLiveElsewhere`."""
        params = {
            "t": self.tenant_id,
            "m": marketplace,
            "seller": seller_id,
            "region": region,
            "scopes": list(scopes),
            "enc": sealed.blob,
            "kv": sealed.key_version,
            "expires_at": expires_at,
            "user": connected_by,
        }
        refused = False
        try:
            new_id = (await self.db.execute(_INSERT_LIVE, params)).scalar_one_or_none()
        except IntegrityError as exc:
            name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if name != UQ_MA_SELLER_LIVE:
                raise
            refused = True
        if refused:
            # raised outside the handler: the driver's DETAIL never rides along
            raise SellerLiveElsewhere
        if new_id is None:
            return None
        return await self.get_by_id(new_id)

    async def reconnect(
        self,
        account_id: uuid.UUID,
        *,
        sealed: SealedTokens,
        expires_at: datetime | None,
        region: str | None,
        scopes: tuple[str, ...],
        user_id: uuid.UUID,
    ) -> None:
        """The same seller connected again: new tokens, back to `active`, same id."""
        result = await self.db.execute(
            _RECONNECT,
            {
                "t": self.tenant_id,
                "id": account_id,
                "enc": sealed.blob,
                "kv": sealed.key_version,
                "expires_at": expires_at,
                "region": region,
                "scopes": list(scopes),
                "user": user_id,
            },
        )
        if _rowcount(result) != 1:
            raise RuntimeError("reconnect found no live account")
        self._expire(account_id)

    async def revoke(self, account_id: uuid.UUID) -> None:
        result = await self.db.execute(_REVOKE, {"t": self.tenant_id, "id": account_id})
        if _rowcount(result) != 1:
            raise RuntimeError("revoke found no live account")
        self._expire(account_id)

    def _expire(self, account_id: uuid.UUID) -> None:
        for obj in builtins.list(self.db.identity_map.values()):
            if isinstance(obj, MarketplaceAccount) and obj.id == account_id:
                self.db.expire(obj)

    async def active_for_publish(
        self, marketplace: str, account_id: uuid.UUID | None = None
    ) -> uuid.UUID | None:
        """The `active` account a publish uses (S21 D6; the 0004 publish gate's rule): the
        listing's own account once set, else the tenant's active one for the marketplace."""
        m = MarketplaceAccount
        stmt = (
            self._select()
            .where(m.marketplace == marketplace, m.status == "active")
            .with_only_columns(m.id)
        )
        if account_id is not None:
            stmt = stmt.where(m.id == account_id)
        found: uuid.UUID | None = (await self.db.execute(stmt.limit(1))).scalar_one_or_none()
        return found

    async def revoked_ids(self, marketplace: str) -> builtins.list[uuid.UUID]:
        """The tenant's revoked accounts for `marketplace` (a connect adopts their listings)."""
        m = MarketplaceAccount
        stmt = (
            self._select()
            .where(m.marketplace == marketplace, m.status == "revoked")
            .with_only_columns(m.id)
            .order_by(m.id)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def status_for_share(self, account_id: uuid.UUID) -> str | None:
        """The account's status, read `FOR SHARE`, so a concurrent revoke (which takes the row
        `FOR UPDATE`) lands either before this read or after the caller's commit."""
        m = MarketplaceAccount
        stmt = (
            self._select()
            .where(m.id == account_id)
            .with_only_columns(m.status)
            .with_for_update(read=True)
        )
        found: str | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return found

    async def active_by_id(self, account_id: uuid.UUID) -> AccountState | None:
        """The tenant's account `account_id` in any status (the caller refuses one that is not
        active), or None, also for another tenant's id. Never selects `token_enc`."""
        m = MarketplaceAccount
        stmt = (
            self._select()
            .where(m.id == account_id)
            .with_only_columns(m.id, m.marketplace, m.seller_id, m.status)
        )
        row = (await self.db.execute(stmt)).one_or_none()
        if row is None:
            return None
        return AccountState(row.id, row.marketplace, row.seller_id, row.status)

    async def snapshot(self, account_id: uuid.UUID, *, margin: timedelta) -> RefreshSnapshot | None:
        row = (
            await self.db.execute(
                _SNAPSHOT, {"t": self.tenant_id, "id": account_id, "margin": margin}
            )
        ).one_or_none()
        if row is None:
            return None
        return RefreshSnapshot(
            id=row.id,
            marketplace=row.marketplace,
            status=row.status,
            token_enc=None if row.token_enc is None else bytes(row.token_enc),
            kms_key_version=row.kms_key_version,
            expires_at=row.expires_at,
            updated_at=row.updated_at,
            due=bool(row.due),
        )

    async def cas_tokens(
        self,
        account_id: uuid.UUID,
        seen_updated_at: datetime,
        sealed: SealedTokens,
        expires_at: datetime | None,
    ) -> bool:
        result = await self.db.execute(
            _CAS_TOKENS,
            {
                "t": self.tenant_id,
                "id": account_id,
                "seen": seen_updated_at,
                "enc": sealed.blob,
                "kv": sealed.key_version,
                "expires_at": expires_at,
            },
        )
        return _rowcount(result) == 1

    async def cas_needs_reconnect(self, account_id: uuid.UUID, seen_updated_at: datetime) -> bool:
        result = await self.db.execute(
            _CAS_NEEDS_RECONNECT, {"t": self.tenant_id, "id": account_id, "seen": seen_updated_at}
        )
        return _rowcount(result) == 1

    async def cas_error(self, account_id: uuid.UUID, seen_updated_at: datetime, code: str) -> bool:
        result = await self.db.execute(
            _CAS_ERROR,
            {"t": self.tenant_id, "id": account_id, "seen": seen_updated_at, "code": code},
        )
        return _rowcount(result) == 1


@asynccontextmanager
async def refresh_lock(account_id: uuid.UUID) -> AsyncIterator[bool]:
    """A session-level `pg_try_advisory_lock` for one account's refresh on its own AUTOCOMMIT
    connection (no transaction is held); yields whether this caller holds it, and unlocks in
    `finally`. A crashed worker's connection closes, which releases the lock."""
    if not isinstance(account_id, uuid.UUID):
        raise ValueError("account_id must be a UUID")
    key = f"ma_refresh:{account_id}"
    async with get_engine().connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        held = bool((await conn.execute(_TRY_LOCK, {"key": key})).scalar())
        try:
            yield held
        finally:
            if held:
                await conn.execute(_UNLOCK, {"key": key})


async def due_accounts(
    session: Any,
    *,
    margin: timedelta,
    limit: int = 500,
    marketplaces: Sequence[str] | None = None,
) -> builtins.list[tuple[uuid.UUID, uuid.UUID]]:
    """`(tenant_id, id)` of every `active` account whose token expires within `margin`, in a
    `trial`/`active` tenant, soonest first, at most `limit`; with `marketplaces`, only accounts of
    those (S22 D5: a switched-off marketplace is not refreshed). Cross-tenant: run it only in
    `platform_scope()` (the periodic refresh). Reads no credential column."""
    model = MarketplaceAccount
    wanted = list(marketplaces) if marketplaces is not None else ["amazon", "flipkart"]
    stmt = (
        select(model.tenant_id, model.id)
        .where(
            model.status == "active",
            model.marketplace.in_(wanted),
            model.expires_at < func.now() + margin,
            exists(
                select(Tenant.id).where(
                    Tenant.id == model.tenant_id, Tenant.status.in_(LIVE_TENANT)
                )
            ),
        )
        .order_by(model.expires_at, model.id)
        .limit(limit)
    )
    return [(row.tenant_id, row.id) for row in (await session.execute(stmt)).all()]
