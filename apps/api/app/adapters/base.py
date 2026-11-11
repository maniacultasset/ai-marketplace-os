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
- SI2: `SellerFacts` are the seller's own factual values one item sends (price, MRP, currency,
  country, manufacturer, composition), keyed by product id in `AdapterListing.unit_facts`; they
  are reported at `facts.<key>` (`fact_paths`). `composition_text`, `money`, `locator_ok` and
  `nearest_values` are the pure helpers both adapters share. `party_secrets(listing)` lists the
  manufacturer strings an issue text must never carry, and `issue_text` replaces any issue on a
  manufacturer attribute with a fixed text (DPDP, `db/models/facts.py:88`).
- SI8: `SellerFacts` also carry the style-owned identity facts (`brand_name`, `gender`,
  `style_number`; `STYLE_FACT_KEYS`), reported at `facts.<key>` for every role. `unit_images`
  is the one rule for which version images an item sends: a colour child sends its own
  colourway's images (main first), then the style-level images, never another colourway's.
- `SellerFacts` also carry the packed box (`package_*`, cm and g). Each adapter decides whether
  it sends them: Amazon does on sellable items, Flipkart never in v1.
"""

from __future__ import annotations

import difflib
import functools
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, get_args
from uuid import UUID

from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.paths import is_valid_field_path
from mkos_schemas.publish import ItemResult, ListingResult
from pydantic import SecretStr

from app.core.redaction import REDACTED

ItemRole = Literal["standalone", "parent", "child"]
ITEM_ROLES: Final = frozenset(get_args(ItemRole))
MAX_ATTEMPTS: Final = 3
DIGEST_BYTES: Final = 32
_EMPTY: Final[Mapping[Any, Any]] = MappingProxyType({})
FACT_SPEC_KEYS: Final = ("country_of_origin", "fiber_composition", "manufacturer")
"""The seller facts an adapter maps through the template spec of the same canonical key."""
STYLE_FACT_KEYS: Final = ("brand_name", "gender", "style_number")
"""SI8: the style-owned identity facts every item answers for at `facts.<key>` (a child's are
always its style's, `ck_products_style_owned`)."""
PRICE_PATHS: Final = ("facts.mrp", "facts.selling_price")
_CENT: Final = Decimal("0.01")
_HUNDRED: Final = Decimal(100)
# scheme :// authority rest (adapters import no URL or socket library, `test_fake_adapter`)
_URL: Final = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)", re.DOTALL)
_PORT: Final = re.compile(r":[0-9]*$")


@dataclass(frozen=True)
class SellerFacts:
    """The seller's own factual values one item sends (SI2 design 2.1). Money is quantized to
    0.01. `__repr__` leaves out the manufacturer address (DPDP, `db/models/facts.py:88`)."""

    mrp: Decimal | None = None
    selling_price: Decimal | None = None
    currency: str | None = None
    country_of_origin: str | None = None
    manufacturer_name: str | None = None
    manufacturer_address: str | None = field(default=None, repr=False)
    fiber_composition_text: str | None = None
    fiber_composition_parts: object | None = None
    brand_name: str | None = None
    """SI8: the effective brand (own, style, brand profile, tenant name)."""
    gender: str | None = None
    """SI8: the audience (`products.gender`, style-owned)."""
    style_number: str | None = None
    """SI8: the style number (style-owned)."""
    # SI5 B1: the item's barcode (`products.identifier_*`), or the seller's GTIN exemption
    identifier_type: str | None = None
    identifier_value: str | None = None
    identifier_exempt: bool = False
    # The packed box as shipped (`products.package_*`, a variant's own else its style's): the
    # three sides in cm, all or none (`ck_products_dims_all_or_none`), and the weight in grams.
    package_length_cm: Decimal | None = None
    package_breadth_cm: Decimal | None = None
    package_height_cm: Decimal | None = None
    package_weight_g: int | None = None


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
class PrecheckResult:
    """SI13: what one catalog and restriction pre-check found: the issue items, the (sku, asin)
    pairs the marketplace already lists under the seller's brand, and the SKUs whose check failed
    (never blocking)."""

    items: tuple[ErrorItem, ...] = ()
    matched: tuple[tuple[str, str], ...] = ()
    failed: tuple[str, ...] = ()
    checked: int = 0
    """How many units were searched."""
    scope: tuple[str, ...] = ()
    """The identifier paths of the units checked with an answer, so the next check closes a row
    it no longer finds; a unit whose check failed keeps its rows."""


@dataclass(frozen=True)
class AdapterTemplate:
    """The listing version's pinned template. `product_type` NULL means the category is not
    mapped for this marketplace."""

    product_type: str | None
    external_category_id: str | None
    template: CategoryTemplate


@dataclass(frozen=True)
class ProductTypeSchema:
    """The current Amazon Product Type Definition a listing is checked against (DW2). A light row:
    it never holds the schema JSON (C6). `validator` is an opaque handle set only by
    `app.adapters.amazon.schema_check.prepare()`; None means not prepared (the check then fails
    closed with one `SCHEMA_INVALID`)."""

    definition_id: UUID
    product_type: str
    product_type_version: str
    requirements: str
    enforced: bool
    """`requirementsEnforced == "ENFORCED"`: a missing required attribute is then an error."""
    digest: bytes
    """sha256 of the canonical schema JSON, 32 bytes: the validator cache key."""
    validator: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.product_type, str) or not self.product_type.strip():
            raise ValueError("product_type must be a non-blank string")
        if not isinstance(self.product_type_version, str) or not self.product_type_version.strip():
            raise ValueError("product_type_version must be a non-blank string")
        if not isinstance(self.digest, bytes) or len(self.digest) != DIGEST_BYTES:
            raise ValueError("digest must be 32 bytes")


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
class AdapterRepair:
    """SI11 (ADR-0028): one stored adapter-side repair of one marketplace attribute. The adapter
    applies it to a unit only while `sha256` of that unit's source value at `field_path` still
    equals `source_digest` (the seller has not changed it since); `value` None drops the
    attribute (`drop_locked`). `seller_sku` None means every unit of the listing."""

    id: UUID
    field_path: str
    attribute: str
    rule: str
    source_digest: bytes
    to_digest: bytes
    value: tuple[Mapping[str, Any], ...] | None
    seller_sku: str | None = None


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
    colour_image_ids: Mapping[str, tuple[UUID, ...]] = field(default_factory=lambda: _EMPTY)
    """colour_key -> the ids of that colourway's images found in the version (S22 C1; SI8
    CP-D6); `unit_images` sends each child its own colour's images plus the style-level ones."""
    product_type_schema: ProductTypeSchema | None = None
    """DW2: the stored Amazon definition to check each item against; None = no check (AC10).
    SI2: the `LISTING` definition, for sellable items (standalone, child)."""
    unit_facts: Mapping[UUID, SellerFacts] = field(default_factory=lambda: _EMPTY)
    """SI2: product id -> the seller facts that item sends; a unit with none fails closed."""
    parent_type_schema: ProductTypeSchema | None = None
    """SI2: the `LISTING_PRODUCT_ONLY` definition, for a family's parent; None = no check."""
    allow_http_images: bool = False
    """SI2: let an `http://` image locator through (APP_ENV=local only; the caller sets it)."""
    live: bool = False
    """SI9 B4 with SI5 CP-D14: the listing has a published version, so its next full send is an
    update; a preview or check of it does not refuse an item with no barcode (an update never
    does), so the pre-approval check agrees with what publish will send."""
    repairs: tuple[AdapterRepair, ...] = ()
    """SI11: the listing's live adapter-side repairs (`listing_repairs`), applied while their
    source value is unchanged; a stale one is reported, never applied."""

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
            "colour_image_ids",
            MappingProxyType({k: tuple(v) for k, v in self.colour_image_ids.items()}),
        )
        object.__setattr__(self, "unit_facts", MappingProxyType(dict(self.unit_facts)))
        object.__setattr__(self, "repairs", tuple(self.repairs))
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


class MarketplaceDisabled(AdapterError):  # noqa: N818 - the plan's name (S22 C4)
    """The marketplace is switched off here (`FLIPKART_ENABLED=false`): nothing was unsealed or
    sent. The report is the 501 NOT_IMPLEMENTED refusal (source `system`)."""


class PublishingPaused(MarketplaceDisabled):
    """The publish kill switch is off (`AMAZON_PUBLISH_ENABLED=false`): a write call was refused
    before the limiter, the token or the network, so nothing was sent. The report is the 501
    NOT_IMPLEMENTED pause (`app.core.flags.publishing_paused_report`)."""


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


def money(value: str | None) -> Decimal | None:
    """A decimal string as Decimal quantized to 0.01; None for None. Raises ValueError for NaN,
    Infinity or a non-number."""
    if value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not number.is_finite():
        raise ValueError("not a finite number")
    return number.quantize(_CENT)


def _percent(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise ValueError("a percent must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("a percent must be finite")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("a percent must be a number") from None
    if not number.is_finite() or number <= 0 or number > _HUNDRED:
        raise ValueError("a percent is above 0 and at most 100")
    return number


def _parts(parts: object) -> list[tuple[str, Decimal]]:
    """SI1 seam S-c: a non-empty list of `{"fiber"|"fibre": str, "percent": number}` summing
    to exactly 100, or ValueError."""
    if not isinstance(parts, list) or not parts:
        raise ValueError("the composition must be a non-empty list")
    out: list[tuple[str, Decimal]] = []
    for part in parts:
        if not isinstance(part, Mapping):
            raise ValueError("each composition part is an object")
        name = part.get("fiber", part.get("fibre"))
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a fibre name is blank")
        out.append((name, _percent(part.get("percent"))))
    if sum((p for _, p in out), Decimal(0)) != _HUNDRED:
        raise ValueError("the percents must add up to 100")
    return out


def _title(name: str) -> str:
    words = unicodedata.normalize("NFKC", name).split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _percent_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def composition_text(text: str | None, parts: object | None) -> str | None:
    """The composition one item sends: the seller's raw text (trimmed) when present, else the
    parts written as `60% Cotton, 40% Polyester`. `parts`, when given, is always checked first
    (ValueError on a bad shape or a sum other than 100)."""
    checked = None if parts is None else _parts(parts)
    if isinstance(text, str) and text.strip():
        return text.strip()
    if checked is None:
        return None
    return ", ".join(f"{_percent_text(p)}% {_title(n)}" for n, p in checked)


def _clean_locator(ref: str) -> bool:
    return bool(ref) and not any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in ref)


def locator_ok(ref: str, *, preview: bool, allow_http: bool) -> bool:
    """An image locator an item may send: an absolute https URL with a host; `http://` only when
    `allow_http` (local); the preview tag `image:<uuid>` only in a preview."""
    if not isinstance(ref, str) or not _clean_locator(ref):
        return False
    if ref.startswith("image:"):
        if not preview:
            return False
        try:
            UUID(ref.removeprefix("image:"))
        except ValueError:
            return False
        return True
    match = _URL.fullmatch(ref)
    if match is None:
        return False
    scheme = match.group(1).casefold()
    host = _PORT.sub("", match.group(2).rsplit("@", 1)[-1])
    if not host:
        return False
    return scheme == "https" or (scheme == "http" and allow_http)


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def nearest_values(value: str, allowed: Sequence[str], n: int = 5) -> list[str]:
    """Up to `n` allowed values close to `value` (deterministic); the first `n` when none is."""
    folded = {_fold(a): a for a in reversed(list(allowed))}
    close = difflib.get_close_matches(_fold(value), list(folded), n=n, cutoff=0.5)
    if close:
        return [folded[c] for c in close]
    return list(allowed)[:n]


@dataclass(frozen=True)
class UnitImage:
    """One image an item sends: its position in the version's `images` (the `images[<i>]` path)
    and its locator (None when the image has none)."""

    index: int
    locator: str | None


def unit_images(listing: AdapterListing, unit: AdapterItem) -> tuple[UnitImage, ...]:
    """The version images one item sends, in send order (SI8 T1; pure, no cut: each adapter
    cuts at its own maximum).

    - Not a child, or a child with no colourway key: every version image, in order.
    - A colour child: `own` = its colourway's images (`colour_image_ids`) in version order with
      the `main` slot first; `style` = the images in no colourway at all.
      - `own` empty: `style`, the style main included;
      - `own` with a main: `own`, then `style` without its `main` images;
      - `own` without a main (CP-D5): the style `main` images, then `own`, then the rest.
    - Never an image of another colourway; each image at most once.
    """
    images = listing.listing.images
    refs = listing.image_refs
    if unit.item_role != "child" or unit.colour_key is None:
        return tuple(UnitImage(i, refs.get(img.image_id)) for i, img in enumerate(images))
    own_ids = set(listing.colour_image_ids.get(unit.colour_key, ()))
    coloured = {i for ids in listing.colour_image_ids.values() for i in ids}
    own = sorted(
        (i for i, img in enumerate(images) if img.image_id in own_ids),
        key=lambda i: images[i].slot is not ImageSlot.MAIN,
    )
    style = [i for i, img in enumerate(images) if img.image_id not in coloured]
    style_main = [i for i in style if images[i].slot is ImageSlot.MAIN]
    style_rest = [i for i in style if images[i].slot is not ImageSlot.MAIN]
    if not own:
        order = style
    elif images[own[0]].slot is ImageSlot.MAIN:
        order = own + style_rest
    else:
        order = style_main + own + style_rest
    seen: set[UUID] = set()
    out: list[UnitImage] = []
    for i in order:
        image_id = images[i].image_id
        if image_id in seen:
            continue
        seen.add(image_id)
        out.append(UnitImage(i, refs.get(image_id)))
    return tuple(out)


def fact_paths(unit: AdapterItem) -> tuple[str, ...]:
    """The `facts.*` paths one item answers for: the compliance and style identity facts for
    every item, the price paths for a sellable one (standalone, child)."""
    paths = {f"facts.{k}" for k in (*FACT_SPEC_KEYS, *STYLE_FACT_KEYS)}
    if unit.item_role != "parent":
        paths |= set(PRICE_PATHS)
    return tuple(sorted(paths))


PARTY_PATH: Final = "facts.manufacturer"
PARTY_REJECTED_MESSAGE: Final = "The manufacturer contact was rejected"
"""The fixed issue text for any marketplace issue on an attribute that carries the manufacturer
contact: a reformatted echo (other separators or case) beats substring scrubbing, so the raw text
stays only in the private raw response log (DPDP, `db/models/facts.py:88`)."""
_PART_MIN: Final = 4  # shorter address parts ("IN", "GJ") would redact unrelated text
_ADDRESS_SPLIT: Final = re.compile(r"\s*[,\n]\s*")


def party_secrets(listing: AdapterListing) -> tuple[str, ...]:
    """Every manufacturer string an issue text must never carry, longest first: each unit's
    name, address and combined `"<name>, <address>"`, plus every run of consecutive address
    parts (split on commas) of at least 4 characters, so a truncated echo is scrubbed too."""
    found: set[str] = set()
    for facts in listing.unit_facts.values():
        name = (facts.manufacturer_name or "").strip()
        address = (facts.manufacturer_address or "").strip()
        if address:
            found.add(address)
            parts = [p for p in _ADDRESS_SPLIT.split(address) if p]
            for i in range(len(parts)):
                for j in range(i + 1, len(parts) + 1):
                    run = ", ".join(parts[i:j])
                    if len(run) >= _PART_MIN:
                        found.add(run)
        if name:
            found.add(name)
        if name and address:
            found.add(f"{name}, {address}")
    return tuple(sorted(found, key=lambda s: (-len(s), s)))


_WORD: Final = re.compile(r"[^\W_]+")


@functools.lru_cache(maxsize=64)
def _loose_patterns(parties: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    out: list[re.Pattern[str]] = []
    for party in parties:
        words = _WORD.findall(party)
        if words and len(party) >= _PART_MIN:
            out.append(re.compile(r"[\W_]+".join(map(re.escape, words)), re.IGNORECASE))
    return tuple(out)


def party_patterns(parties: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """Loose matchers for the `party_secrets` strings (same order, longest first): the words
    of each string of at least 4 characters, case-insensitive, joined by any run of
    non-alphanumerics, so `"TEST MILLS PVT LTD; 1 MILL ROAD / SURAT"` still matches."""
    return _loose_patterns(tuple(parties))


def issue_text(
    raw: str,
    names: Sequence[str],
    index: Mapping[str, Sequence[str]],
    parties: Sequence[str] = (),
) -> str:
    """The marketplace text to keep for one issue: `PARTY_REJECTED_MESSAGE` when any attribute
    it names feeds `facts.manufacturer` (in scope or not), else `raw` with every loose echo of
    the manufacturer strings (`parties`, from `party_secrets`) redacted, whatever it names."""
    if any(PARTY_PATH in index.get(n, ()) for n in names):
        return PARTY_REJECTED_MESSAGE
    for pattern in party_patterns(parties):
        raw = pattern.sub(REDACTED, raw)
    return raw


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


RemoteStatus = Literal["clean", "issues", "failed"]


@dataclass(frozen=True)
class RemotePreview:
    """The marketplace's dry-run verdict on what a create would send (SI11; Amazon
    `VALIDATION_PREVIEW`, which saves nothing). `status`: `clean` (every item VALID), `issues`
    (an item INVALID: `errors` holds the mapped, scrubbed items) or `failed` (nothing judged: a
    local ERROR sent nothing, a 4xx, or a reply the adapter could not read). `applied` is True
    only when the marketplace answered as if it stored the item (an invariant-1 alarm: the
    caller opens the account breaker). `scope` holds every non-image path the items carried;
    `per_sku` the raw verdict per seller SKU (`VALID`, `INVALID`, or what came back)."""

    status: RemoteStatus
    applied: bool = False
    errors: tuple[ErrorItem, ...] = ()
    per_sku: Mapping[str, str] = field(default_factory=lambda: _EMPTY)
    scope: tuple[str, ...] = ()


class RemotePreviewer(Protocol):
    """The one call the dry-run job may make (SI11): a preview, never a write."""

    async def preview_remote(self, item: AdapterListing) -> RemotePreview: ...


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
