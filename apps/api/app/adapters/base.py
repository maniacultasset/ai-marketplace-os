"""The marketplace adapter interface (spec L6516-6538, L10529-10533; invariant 4; S14b T10).

Marketplace logic lives only in adapters: each takes the canonical listing and answers per field.
An adapter owns its auth, rate limits, idempotency and error mapping, and returns a
`ListingResult` from every listing call: never a bool. A partial result keeps each failed field's
raw marketplace code and text (`ErrorItem.raw_code`, `message`).

- `AdapterListing`: what a listing call carries (the canonical listing, its seller SKU and the
  idempotency key of the attempt).
- `PerformanceDay`: one day of listing performance, in S7's `performance` columns.
- `submitted_fields(listing)`: the field paths one submission answers for, each a legal
  `FieldResult` path (`images[<i>]`, `keywords.<list>`; never the rule-only `images`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import is_valid_field_path
from mkos_schemas.publish import ListingResult


@dataclass(frozen=True)
class AdapterListing:
    listing_id: UUID
    seller_sku: str
    idempotency_key: str
    listing: CanonicalListing


@dataclass(frozen=True)
class PerformanceDay:
    date: date
    impressions: int
    clicks: int
    orders: int
    returns: int
    revenue: Decimal | None


class ListingNotFound(Exception):  # noqa: N818 - the plan's name (S14b T10)
    """The marketplace holds no listing for this seller SKU."""


def submitted_fields(listing: CanonicalListing) -> list[str]:
    """The sorted field paths a submission of `listing` answers for."""
    fields = ["content.title", "content.bullets", "content.description"]
    if listing.keywords.search_terms:
        fields.append("keywords.search_terms")
    if listing.keywords.hidden_terms:
        fields.append("keywords.hidden_terms")
    fields += [f"attributes.{key}" for key in listing.attributes]
    fields += [f"images[{i}]" for i in range(len(listing.images))]
    bad = [f for f in fields if not is_valid_field_path(f)]
    if bad:
        raise ValueError(f"illegal field path(s) {bad}")
    return sorted(fields)


class MarketplaceAdapter(ABC):
    """One marketplace. Every listing method returns a `ListingResult`."""

    @abstractmethod
    async def get_listing(self, seller_sku: str) -> ListingResult: ...

    @abstractmethod
    async def create_listing(self, item: AdapterListing) -> ListingResult: ...

    @abstractmethod
    async def update_listing(
        self, item: AdapterListing, *, fields: Sequence[str]
    ) -> ListingResult: ...

    @abstractmethod
    async def publish_listing(self, item: AdapterListing) -> ListingResult: ...

    @abstractmethod
    async def get_performance(
        self, listing_id: UUID, from_date: date, to_date: date
    ) -> list[PerformanceDay]: ...
