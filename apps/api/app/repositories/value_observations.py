"""`ValueObservationRepository`: one tenant's closed marketplace `VALUE_NOT_ALLOWED` refusals of an
attribute on a listing that later went live (SI13 T15; P:B4, D7; CP-D12).

Each row is `(marketplace, pinned product type, key, v, w, child, listing id)`: `v` the value the
refused version sent (`attributes.<key>`; a child item's refusal keeps the value the marketplace
echoed in `value_submitted`), `w` the published version's value (listing level only; a child's is
None). Strings only. Read in the tenant's own unit of work (RLS and the tenant criterion); the CLI
aggregates tenants in memory and prints no id.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from sqlalchemy import select

from app.db.models.issues import ListingIssue
from app.db.models.listings import Listing, ListingVersion
from app.db.models.platform import CategoryTemplate
from app.repositories.base import TenantScopedRepository

ADAPTERS: Final = ("amazon_adapter", "flipkart_adapter")
LIMIT_MAX: Final = 500


@dataclass(frozen=True, slots=True)
class RefusedValue:
    marketplace: str
    product_type: str | None
    key: str
    v: str
    w: str | None
    child: bool
    listing_id: uuid.UUID


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class ValueObservationRepository(TenantScopedRepository[ListingIssue]):
    model: ClassVar[type[ListingIssue]] = ListingIssue

    async def rejected_then_live(
        self, keys: Sequence[str], limit: int = LIMIT_MAX
    ) -> list[RefusedValue]:
        paths = sorted({f"attributes.{k}" for k in keys})
        if not paths:
            return []
        stmt = self._scoped_read(
            select(
                ListingIssue.field_path,
                ListingIssue.listing_item_id,
                ListingIssue.value_submitted,
                ListingIssue.listing_version_id,
                Listing.id.label("listing_id"),
                Listing.marketplace,
                Listing.published_version_id,
            )
            .join(Listing, Listing.id == ListingIssue.listing_id)
            .where(
                self._criterion_for(ListingIssue),
                self._criterion_for(Listing),
                ListingIssue.status != "open",
                ListingIssue.error_code == "VALUE_NOT_ALLOWED",
                ListingIssue.source.in_(ADAPTERS),
                ListingIssue.field_path.in_(paths),
                Listing.published_version_id.is_not(None),
            )
            .order_by(ListingIssue.created_at, ListingIssue.id)
            .limit(max(1, min(limit, LIMIT_MAX)))
        )
        rows = list(await self.db.execute(stmt))
        ids = {r.listing_version_id for r in rows} | {r.published_version_id for r in rows}
        ids.discard(None)
        versions: dict[uuid.UUID, Any] = {}
        if ids:
            vstmt = self._scoped_read(
                select(
                    ListingVersion.id,
                    ListingVersion.content_json,
                    ListingVersion.category_template_id,
                ).where(self._criterion_for(ListingVersion), ListingVersion.id.in_(sorted(ids)))
            )
            versions = {r.id: r for r in await self.db.execute(vstmt)}
        template_ids = {v.category_template_id for v in versions.values()} - {None}
        types: dict[uuid.UUID, str | None] = {}
        if template_ids:
            tstmt = select(CategoryTemplate.id, CategoryTemplate.product_type).where(
                CategoryTemplate.id.in_(sorted(template_ids))
            )
            types = {r.id: r.product_type for r in await self.db.execute(tstmt)}
        out: list[RefusedValue] = []
        for row in rows:
            key = str(row.field_path).removeprefix("attributes.")
            child = row.listing_item_id is not None
            refused = versions.get(row.listing_version_id)
            published = versions.get(row.published_version_id)
            sent = (
                ((refused.content_json if refused else None) or {}).get("attributes") or {}
            ).get(key)
            live = (
                ((published.content_json if published else None) or {}).get("attributes") or {}
            ).get(key)
            v = _text(row.value_submitted) if child else (_text(sent) or _text(row.value_submitted))
            if v is None:
                continue
            out.append(
                RefusedValue(
                    marketplace=str(row.marketplace),
                    product_type=(
                        types.get(published.category_template_id) if published is not None else None
                    ),
                    key=key,
                    v=v,
                    w=None if child else _text(live),
                    child=child,
                    listing_id=row.listing_id,
                )
            )
        return out
