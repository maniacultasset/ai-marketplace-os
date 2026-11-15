"""The grouped Inbox, pure (G1, prod bulk test 15 Nov 2026: 34 products drafted at once opened the
same question 34 times). No I/O, no clock: `app.services.inbox_groups` reads the rows and calls in.

- **Group key.** Identical open items across the brand's products (a tenant is a brand) share one
  key, built from the row alone, so the answer can re-derive it from each task it is sent:
  - a question: `t|<kind>|<field family of target_path>|<spec12>`, `spec12` the first 12 hex of
    sha256 over the canonical JSON of `expected_answer`. Questions whose categories allow other
    values land in other groups, so one answer input always fits every member of a group;
  - an issue: `i|<error_code>|<field family of field_path>|<marketplace>|<severity>`.
  A key past 200 characters is replaced by `h|<sha256>` of itself (still derived from the row).
- **Scope.** What one answer for the group may do: `brand` (the brand manufacturer, set once
  through `PATCH /brand-profile`), `product_fact` (one value written to each product that has
  none), `exemption` (the GTIN exemption, D1) or `per_item` (each member needs its own fix).
  A conflicting-facts question is `per_item`: each product already holds a value the photos
  disagree with, and an answer for many must never overwrite one.
- **Has value.** Whether a member's product already holds the fact (its own value, or its
  style's for a variant): such a member is kept as it is, never overwritten.
- **Order.** Blocking groups first, then the biggest, then the newest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

Scope = Literal["brand", "product_fact", "exemption", "per_item"]
ItemKind = Literal["task", "issue"]

KEY_MAX: Final = 200
GROUPS_MAX: Final = 300
MEMBERS_MAX: Final = 1000
CATEGORIES_MAX: Final = 20
MEMBER_LISTINGS_MAX: Final = 4
ANSWER_IDS_MAX: Final = 100

FACT_KINDS: Final = frozenset({"missing_fact"})
COLUMNS: Final = ("gender", "brand_name", "hsn_code", "country_of_origin")
PRODUCT_FACT_TARGET: Final = re.compile(
    r"^facts\.(?:attributes\.([a-z0-9_]{1,64})|(gender|brand_name|hsn_code|country_of_origin))$"
)
"""The targets `services.tasks.FACT_TARGET` answers (a unit test keeps the two equal)."""
SPEC_KINDS: Final = frozenset(
    {"enum", "text", "number", "boolean", "money_inr", "percent_composition"}
)
MANUFACTURER_TARGET: Final = "facts.manufacturer"
IDENTIFIER_TARGET: Final = "facts.identifier"
_INDEX: Final = re.compile(r"\[[^\]]*\]")


def field_family(path: str | None) -> str:
    """`images[3]` -> `images[]`, `variants[SKU-1].facts.color` -> `variants[].facts.color`."""
    return _INDEX.sub("[]", path or "")


def _canonical_spec(spec: Any) -> Any:
    """The spec with its allowed values in a stable order (the same list in another order is
    the same answer)."""
    if not isinstance(spec, Mapping):
        return spec
    out = dict(spec)
    for name in ("allowed_values", "allowed"):
        values = out.get(name)
        if isinstance(values, list):
            out[name] = sorted(values, key=lambda v: json.dumps(v, sort_keys=True))
    return out


def spec_digest(spec: Any) -> str:
    text = json.dumps(_canonical_spec(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _bounded(key: str) -> str:
    if len(key) <= KEY_MAX:
        return key
    return "h|" + hashlib.sha256(key.encode()).hexdigest()


def task_key(kind: str, target_path: str | None, expected: Any) -> str:
    return _bounded(f"t|{kind}|{field_family(target_path)}|{spec_digest(expected)}")


def issue_key(error_code: str, field_path: str, marketplace: str, severity: str) -> str:
    return _bounded(f"i|{error_code}|{field_family(field_path)}|{marketplace}|{severity}")


def task_scope(kind: str, target_path: str | None, expected: Any) -> Scope:
    target = target_path or ""
    if target == MANUFACTURER_TARGET:
        return "brand"
    if kind not in FACT_KINDS:
        return "per_item"
    if target == IDENTIFIER_TARGET:
        return "exemption"
    spec_kind = expected.get("kind") if isinstance(expected, Mapping) else None
    if PRODUCT_FACT_TARGET.match(target) and spec_kind in SPEC_KINDS:
        return "product_fact"
    return "per_item"


def task_field(target_path: str | None) -> str | None:
    """The dictionary key a question asks about (`facts.attributes.gender` -> `gender`,
    `facts.identifier` -> `identifier`), else the field family; None without a target."""
    if not target_path:
        return None
    for root in ("facts.attributes.", "facts."):
        if target_path.startswith(root) and "[" not in target_path:
            return target_path.removeprefix(root)
    return field_family(target_path)


# ---- has value: the never-overwrite rule ----


@dataclass(frozen=True, slots=True)
class FactRow:
    """What one product row holds for the answerable facts."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    is_style: bool
    attributes: Mapping[str, Any]
    columns: Mapping[str, Any]
    """`gender`, `brand_name`, `hsn_code`, `country_of_origin`, `manufacturer_party_id`,
    `identifier_status`."""
    variants_without_status: int = 0
    """For a style: its live variants with no barcode and no exemption."""


def _filled(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _attribute(row: FactRow | None, key: str) -> bool:
    if row is None:
        return False
    fact = row.attributes.get(key) if isinstance(row.attributes, Mapping) else None
    return isinstance(fact, Mapping) and _filled(fact.get("value"))


def _column(row: FactRow | None, column: str) -> bool:
    return row is not None and _filled(row.columns.get(column))


def has_value(target_path: str | None, own: FactRow | None, style: FactRow | None) -> bool:
    """True when the product (or, for a variant, its style) already holds the fact the question
    asks: an answer for many products never writes over it. The GTIN exemption counts as held
    when the row has a status (a barcode, or exempt already) or, for a style, when every live
    variant has one."""
    target = target_path or ""
    if own is None:
        return False
    if target == IDENTIFIER_TARGET:
        if own.is_style:
            return own.variants_without_status == 0
        return _column(own, "identifier_status")
    if target == MANUFACTURER_TARGET:
        return _column(own, "manufacturer_party_id") or _column(style, "manufacturer_party_id")
    found = PRODUCT_FACT_TARGET.match(target)
    if found is None:
        return False
    key, column = found.group(1), found.group(2)
    if key is not None:
        return _attribute(own, key) or _attribute(style, key)
    return _column(own, column) or _column(style, column)


# ---- grouping ----


@dataclass(frozen=True, slots=True)
class Member:
    key: str
    item_kind: ItemKind
    scope: Scope
    at: datetime
    blocking: bool
    item_id: uuid.UUID
    field: str | None = None
    task_kind: str | None = None
    error_code: str | None = None
    marketplace: str | None = None
    spec: Mapping[str, Any] | None = None
    product_id: uuid.UUID | None = None
    sku: str | None = None
    title: str | None = None
    category: str | None = None
    listing_ids: tuple[uuid.UUID, ...] = ()
    has_value: bool = False


@dataclass(slots=True)
class Group:
    key: str
    item_kind: ItemKind
    scope: Scope
    field: str | None
    task_kind: str | None
    error_code: str | None
    marketplace: str | None
    answer: Mapping[str, Any] | None
    count: int = 0
    products: int = 0
    listings: int = 0
    blocking: bool = False
    at: datetime | None = None
    categories: list[str] = dataclasses.field(default_factory=list)
    members: list[Member] = dataclasses.field(default_factory=list)
    members_more: bool = False

    @property
    def newest(self) -> Member:
        return self.members[0]


def _newest_first(member: Member) -> tuple[float, str]:
    return (-member.at.timestamp(), str(member.item_id))


def build_groups(members: Iterable[Member]) -> list[Group]:
    """Every member in exactly one group (so the counts add up to the Inbox's), each group's
    members newest first (capped at `MEMBERS_MAX`, `members_more` past it), the groups in
    `order`. The shared fields come from the newest member."""
    buckets: dict[str, list[Member]] = {}
    for member in members:
        buckets.setdefault(member.key, []).append(member)
    groups: list[Group] = []
    for key, found in buckets.items():
        found.sort(key=_newest_first)
        first = found[0]
        products = {m.product_id for m in found if m.product_id is not None}
        listings = {lid for m in found for lid in m.listing_ids}
        categories = sorted({m.category for m in found if m.category})
        groups.append(
            Group(
                key=key,
                item_kind=first.item_kind,
                scope=first.scope,
                field=first.field,
                task_kind=first.task_kind,
                error_code=first.error_code,
                marketplace=first.marketplace,
                answer=first.spec if first.item_kind == "task" else None,
                count=len(found),
                products=len(products),
                listings=len(listings),
                blocking=any(m.blocking for m in found),
                at=first.at,
                categories=categories[:CATEGORIES_MAX],
                members=found[:MEMBERS_MAX],
                members_more=len(found) > MEMBERS_MAX,
            )
        )
    return order(groups)


def order(groups: Sequence[Group]) -> list[Group]:
    """Blocking first, then the biggest, then the newest (then the key, for a stable order)."""

    def rank(g: Group) -> tuple[bool, int, float, str]:
        at = g.at.timestamp() if g.at is not None else 0.0
        return (not g.blocking, -g.count, -at, g.key)

    return sorted(groups, key=rank)


def redraft(status: str, paused: bool) -> bool:
    """Whether a listing can be written again now (`POST /listings/{id}/regenerate` answers 409
    for a listing never written, one being written and a paused one)."""
    return status not in ("created", "generating") and not paused
