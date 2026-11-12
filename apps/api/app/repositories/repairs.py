"""`RepairRepository` (SI11 T10; ADR-0028 proposed): the listing's adapter-side repairs.

A repair is adapter data: one marketplace attribute's value, applied by the adapter only while the
seller's source value still hashes to `source_digest`. `insert` is a replay-safe no-op on either
unique index (`uq_listing_repairs_live`, one live row per listing, item and attribute;
`uq_listing_repairs_once`, one try per listing, issue key and rule, live or retired). A repair is
retired once (`listing_repairs_guard`), never deleted by the app (no DELETE grant).

Client item 20 (0038): a repair found while the brand's auto-fix is off is inserted `held`, a
suggested fix. `live_for_listing` leaves held rows out unless asked (the adapter never applies
one); `release` turns held rows live once, on the seller's "Apply fixes".
"""

from __future__ import annotations

import builtins
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.resolution import RETIRED_REASONS, ListingRepair
from app.repositories.base import TenantScopedRepository

EMPTY_SET: Final = hashlib.sha256(b"").digest()


@dataclass(frozen=True, slots=True)
class NewRepair:
    listing_id: UUID
    marketplace: str
    field_path: str
    attribute: str
    rule: str
    issue_key: str
    source_digest: bytes
    to_digest: bytes
    value_json: Any
    listing_item_id: UUID | None = None
    source_issue_id: UUID | None = None
    job_id: UUID | None = None
    alias_version: int | None = None
    held: bool = False
    """Client item 20: a suggested fix (auto-fix off), applied only once the seller releases it."""


def repair_set_digest(rows: Sequence[ListingRepair]) -> bytes:
    """sha256 of the sorted `(id, to_digest)` pairs of the live repairs (CP-D26): the same
    version with the same repairs is the same payload."""
    pairs = sorted((str(r.id), bytes(r.to_digest).hex()) for r in rows)
    if not pairs:
        return EMPTY_SET
    return hashlib.sha256("\n".join(f"{i}:{d}" for i, d in pairs).encode("ascii")).digest()


class RepairRepository(TenantScopedRepository[ListingRepair]):
    model = ListingRepair

    def _own_session(self) -> None:
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("listing repair write for another tenant's unit of work")

    async def live_for_listing(
        self, listing_id: UUID, *, include_held: bool = False
    ) -> builtins.list[ListingRepair]:
        """The listing's unretired repairs the adapter applies; `include_held` adds the held
        suggestions (the resolution executor, which must see every repair it wrote)."""
        stmt = self._select().where(
            ListingRepair.listing_id == listing_id, ListingRepair.retired_at.is_(None)
        )
        if not include_held:
            stmt = stmt.where(ListingRepair.held.is_(False))
        stmt = stmt.order_by(ListingRepair.created_at, ListingRepair.id)
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def release(self, ids: Sequence[UUID]) -> builtins.list[UUID]:
        """Client item 20: the held, unretired rows among `ids` made live (the seller applied
        them); the ids released, so a replay releases nothing twice."""
        if not ids:
            return []
        self._own_session()
        stmt = self._scoped_write(
            update(ListingRepair)
            .where(
                self._tenant_criterion(include_platform=False),
                ListingRepair.id.in_(builtins.list(ids)),
                ListingRepair.retired_at.is_(None),
                ListingRepair.held.is_(True),
            )
            .values(held=False)
            .returning(ListingRepair.id)
        )
        return [row[0] for row in (await self.db.execute(stmt)).all()]

    async def for_listing(self, listing_id: UUID, limit: int = 50) -> builtins.list[ListingRepair]:
        """Every repair of the listing, live or retired, newest first (the resolution read)."""
        stmt = (
            self._select()
            .where(ListingRepair.listing_id == listing_id)
            .order_by(ListingRepair.created_at.desc(), ListingRepair.id.desc())
            .limit(limit)
        )
        return builtins.list((await self.db.execute(stmt)).scalars())

    async def insert(self, new: NewRepair) -> UUID | None:
        """The new row's id, or None when either unique index already holds it (a replay, a
        second try of the same key, or a live repair of the attribute)."""
        self._own_session()
        stmt = (
            pg_insert(ListingRepair)
            .values(
                tenant_id=self.tenant_id,
                listing_id=new.listing_id,
                marketplace=new.marketplace,
                listing_item_id=new.listing_item_id,
                field_path=new.field_path,
                attribute=new.attribute,
                rule=new.rule,
                issue_key=new.issue_key[:400],
                source_issue_id=new.source_issue_id,
                job_id=new.job_id,
                source_digest=new.source_digest,
                to_digest=new.to_digest,
                value_json=new.value_json,
                alias_version=new.alias_version,
                held=new.held,
            )
            .on_conflict_do_nothing()
            .returning(ListingRepair.id)
        )
        inserted: UUID | None = (await self.db.execute(stmt)).scalar_one_or_none()
        return inserted

    async def retire(self, ids: Sequence[UUID], reason: str) -> int:
        """Retire the live rows among `ids` (a retired row is left as is)."""
        if reason not in RETIRED_REASONS:
            raise ValueError("unknown retired reason")
        if not ids:
            return 0
        self._own_session()
        stmt = self._scoped_write(
            update(ListingRepair)
            .where(
                self._tenant_criterion(include_platform=False),
                ListingRepair.id.in_(builtins.list(ids)),
                ListingRepair.retired_at.is_(None),
            )
            .values(retired_at=func.now(), retired_reason=reason)
            .returning(ListingRepair.id)
        )
        return len((await self.db.execute(stmt)).all())
