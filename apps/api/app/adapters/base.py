"""The marketplace adapter interface (spec L6516-6538, L10529-10533; invariant 4; S14b T10, S20 T1).

Marketplace logic lives only in adapters: each takes the canonical listing and answers per field.
An adapter owns its auth, rate limits, idempotency and error mapping, and returns a
`ListingResult` from every listing call: never a bool. A partial result keeps each failed field's
raw marketplace code and text (`ErrorItem.raw_code`, `message`).

- `AdapterListing`: what a listing call carries: the canonical listing, its seller SKU, the
  idempotency key and attempt (1-3, passed in, never incremented here), the items it publishes
  (one standalone, or a parent then its children: a family is one unit, ADR-0007 §3; or the
  children only for a marketplace with no parent row, S22 C9), the
  neutral variation theme, the pinned category template and the image locators.
- `AdapterItem`: one marketplace item of a listing (`standalone|parent|child`); a child may
  carry its colourway key and colour SKU (S22 C1).
- `AdapterTemplate`: the pinned template with its adapter-only columns (`product_type`).
- `AdapterPreview`: what `preview()` would send, per item, plus the mapping report; network-free.
- Ports the adapter is built with: `TokenSource`, `OutboundLimiter`, `RawResponseSink`.
- `AdapterError` and its subclasses carry the one `ErrorReport` and the items applied before the
  failure (`completed`); their text is the class name only, so no marketplace text, token or
  value reaches a log line or a Sentry event through `str(exc)`.
- `submitted_fields(listing)`: the field paths one submission answers for, each a legal
  `FieldResult` path (`images[<i>]`, `keywords.<list>`; never the rule-only `images`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, get_args
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import is_valid_field_path
from mkos_schemas.publish import ItemResult, ListingResult
from pydantic import SecretStr

ItemRole = Literal["standalone", "parent", "child"]
ITEM_ROLES: Final = frozenset(get_args(ItemRole))
MAX_ATTEMPTS: Final = 3
_EMPTY: Final[Mapping[Any, Any]] = MappingProxyType({})


@dataclass(frozen=True)
class AdapterItem:
    product_id: UUID
    seller_sku: str
    item_role: ItemRole
    variant_attributes: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    colour_key: str | None = None
    """The child's colourway key (`product_colourways.colour_key`, S22 C1); None otherwise."""
    colour_sku: str | None = None
    """The colourway's seller colour SKU, when the seller gave one (S22 C1)."""

    def __post_init__(self) -> None:
        if self.item_role not in ITEM_ROLES:
            raise ValueError("item_role must be standalone, parent or child")
        if not isinstance(self.seller_sku, str) or not self.seller_sku.strip():
            raise ValueError("seller_sku must be a non-blank string")
        object.__setattr__(
            self, "variant_attributes", MappingProxyType(dict(self.variant_attributes))
        )


@dataclass(frozen=True)
class AdapterTemplate:
    """The listing version's pinned template. `product_type` NULL means the category is not
    mapped for this marketplace."""

    product_type: str | None
    external_category_id: str | None
    template: CategoryTemplate


@dataclass(frozen=True)
class PreviewItem:
    """One item `preview()` would send (the wire `PreviewItem` once the contract has it)."""

    product_id: UUID
    seller_sku: str
    item_role: ItemRole
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class AdapterPreview:
    product_type: str | None
    items: tuple[PreviewItem, ...]
    report: ErrorReport


@dataclass(frozen=True)
class AdapterListing:
    listing_id: UUID
    seller_sku: str
    idempotency_key: str
    listing: CanonicalListing
    items: tuple[AdapterItem, ...] = ()
    variation_theme: str | None = None
    template: AdapterTemplate | None = None
    attempt: int = 1
    image_refs: Mapping[UUID, str] = field(default_factory=lambda: _EMPTY)
    colour_image_refs: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: _EMPTY)
    """colour_key -> the image locators of that colourway (S22 C1), in slot order; a family
    whose colourways carry images fans them out to each child of the colour."""

    def __post_init__(self) -> None:
        attempt = self.attempt
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise ValueError("attempt must be an int from 1 to 3")
        if not 1 <= attempt <= MAX_ATTEMPTS:
            raise ValueError("attempt must be an int from 1 to 3")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "image_refs", MappingProxyType(dict(self.image_refs)))
        object.__setattr__(
            self,
            "colour_image_refs",
            MappingProxyType({k: tuple(v) for k, v in self.colour_image_refs.items()}),
        )
        _check_items(self.seller_sku, self.items)

    def units(self) -> tuple[AdapterItem, ...]:
        """The items this listing publishes: its own, or one standalone item of `seller_sku`."""
        if self.items:
            return self.items
        return (AdapterItem(self.listing.product_id, self.seller_sku, "standalone"),)

    @property
    def is_family(self) -> bool:
        return any(i.item_role == "parent" for i in self.items)

    @property
    def is_parentless_family(self) -> bool:
        """Children only, no parent row: a marketplace whose families have no parent item
        (Flipkart, S22 C9; `registry.family_has_parent`)."""
        return bool(self.items) and all(i.item_role == "child" for i in self.items)


def _check_items(seller_sku: str, items: tuple[AdapterItem, ...]) -> None:
    if not items:
        return
    skus = [i.seller_sku for i in items]
    if len(set(skus)) != len(skus):
        raise ValueError("duplicate seller_sku in items")
    roles = [i.item_role for i in items]
    if "standalone" in roles:
        if len(items) != 1 or items[0].seller_sku != seller_sku:
            raise ValueError("a standalone listing has exactly one item, its own seller_sku")
        return
    if all(role == "child" for role in roles):
        return  # a parentless family (S22 C9): the children only, the style SKU stays the key
    if roles[0] != "parent" or items[0].seller_sku != seller_sku:
        raise ValueError("a family's first item is the parent, with the listing's seller_sku")
    if roles.count("parent") != 1:
        raise ValueError("a family has exactly one parent")


@dataclass(frozen=True)
class PerformanceDay:
    date: date
    impressions: int
    clicks: int
    orders: int
    returns: int
    revenue: Decimal | None


# ---- ports ----


class TokenSource(Protocol):
    """The marketplace access token for one account. `refresh()` forces one refresh."""

    async def get(self) -> SecretStr: ...

    async def refresh(self) -> SecretStr: ...


class OutboundLimiter(Protocol):
    """Admits one call of `operation`; returns the admitted window start (epoch seconds)."""

    async def acquire(self, operation: str) -> float: ...


class RawResponseSink(Protocol):
    """Keeps a raw marketplace body (S21 stores it; S20 passes `NullSink`)."""

    def put(self, seller_sku: str, operation: str, body: bytes) -> None: ...


class NullSink:
    def put(self, seller_sku: str, operation: str, body: bytes) -> None:
        return None


# ---- outcomes ----


class ListingNotFound(Exception):  # noqa: N818 - the plan's name (S14b T10)
    """The marketplace holds no listing for this seller SKU."""


class AdapterError(Exception):
    """A marketplace call failed as a whole. `report` is the one ErrorReport; `completed` holds
    the items the marketplace applied before the failure. `str()` is the class name only."""

    def __init__(self, report: ErrorReport, completed: Sequence[ItemResult] = ()) -> None:
        super().__init__()
        self.report = report
        self.completed: tuple[ItemResult, ...] = tuple(completed)

    def __str__(self) -> str:
        return type(self).__name__

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class RateLimited(AdapterError):  # noqa: N818 - the plan's name (S20 C2)
    """429 past the retry budget, or the outbound limiter never admitted the call."""


class AuthExpired(AdapterError):  # noqa: N818 - the plan's name (S20 C2)
    """The token was refused after the one forced refresh, or the refresh was refused."""


class PermissionDenied(AdapterError):  # noqa: N818 - the plan's name (S20 C2)
    """403 with a fresh token: the seller did not grant the needed role."""


class MarketplaceUnavailable(AdapterError):  # noqa: N818 - the plan's name (S20 C2)
    """Definitive: nothing was sent or nothing was applied (connect error, a failed GET)."""


class MarketplaceAmbiguous(AdapterError):  # noqa: N818 - the plan's name (S20 C2)
    """A write was sent and its outcome is unknown (5xx, timeout, a body we cannot read):
    read the listing before any retry (DATABASE.md:871)."""


# ---- helpers ----


def marketplace_failure(
    marketplace: Marketplace | str, code: ErrorCode, message: str
) -> ErrorReport:
    """A whole-call failure as the one ErrorReport, from `<marketplace>_adapter` (invariant 8):
    one `listing` item with the code's class and retry class (S22: the limiter and the Flipkart
    client; the Amazon client keeps its own identical `failure`)."""
    market = Marketplace(marketplace)
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
    item = ErrorItem(
        field="listing",
        error_code=code,
        message=message,
        source=ErrorSource(f"{market.value}_adapter"),
        retryable=retryable,
        marketplace=market,
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )
    return ErrorReport(status="failed", error_code=code, retryable=retryable, errors=[item])


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


UpdateFields = Sequence[str] | Mapping[str, Sequence[str]]


def fields_by_sku(item: AdapterListing, fields: UpdateFields) -> dict[str, list[str]]:
    """`update_listing`'s `fields` as {seller_sku: sorted unique paths}. A plain sequence names
    the listing's own seller SKU (D4)."""
    raw: Mapping[str, Sequence[str]] = (
        {item.seller_sku: fields} if not isinstance(fields, Mapping) else fields
    )
    if not raw:
        raise ValueError("fields must be a non-empty mapping of seller_sku to field paths")
    known = {u.seller_sku for u in item.units()}
    out: dict[str, list[str]] = {}
    for sku, paths in raw.items():
        if sku not in known:
            raise ValueError("unknown seller_sku in fields")
        if isinstance(paths, str):
            raise ValueError("fields must be legal field paths, not a string")
        wanted = sorted(set(paths))
        if not wanted:
            raise ValueError("fields must be a non-empty list of legal field paths")
        if not all(isinstance(p, str) and is_valid_field_path(p) for p in wanted):
            raise ValueError("fields must be a non-empty list of legal field paths")
        out[sku] = wanted
    return out


class MarketplaceReader(Protocol):
    """The one call the status sync may make (DATABASE.md:890; S21 T19): a read, never a write."""

    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult: ...


class MarketplaceAdapter(ABC):
    """One marketplace. Every listing method returns a `ListingResult`."""

    @abstractmethod
    async def get_listing(
        self, seller_sku: str, *, listing_id: UUID | None = None
    ) -> ListingResult: ...

    @abstractmethod
    async def create_listing(self, item: AdapterListing) -> ListingResult: ...

    @abstractmethod
    async def update_listing(
        self, item: AdapterListing, *, fields: UpdateFields
    ) -> ListingResult: ...

    @abstractmethod
    async def publish_listing(self, item: AdapterListing) -> ListingResult: ...

    @abstractmethod
    async def get_performance(
        self, listing_id: UUID, from_date: date, to_date: date
    ) -> list[PerformanceDay]: ...

    @abstractmethod
    def preview(self, item: AdapterListing) -> AdapterPreview:
        """What a create would send, per item, and the mapping report. No I/O."""
