"""Photo views per category and marketplace, and the size chart check (client feedback 7 and 8).

The checklist is data, `services/data/photo_views.json`: for a category (and optionally one
marketplace) each view is `required`, `recommended` or `optional`, with a one-line why. A view is
one the system already stores, so no new slot or role exists for it:

| view | stored as |
|---|---|
| `front` | `main` |
| `back`, `close_up` | `secondary` with that role (SI12) |
| `size_chart` | `size_chart` |
| `lifestyle` | `lifestyle` |
| `packaging_label` | `packaging_label` (read, never published) |

A missing view is left out of a category's list. Lookup, most specific first: (marketplace,
category), (any, category), (marketplace, any), (any, any).

Pure, apart from `load_inputs` (the product's images, saved chart and variant sizes):

- `checklist(marketplace, category)`: the views for one marketplace;
- `merged(category)`: one row per view for the product read and `GET /catalog/requirements`, at
  the strictest requirement any marketplace gives it, with each marketplace's own;
- `view_status(view, inputs)`: `present`, `missing`, or `invalid` (every photo of the view
  failed its checks). The size chart counts as present when the seller saved a chart, typed or
  confirmed from the photo;
- `size_flags(chart, sizes)`: the variant sizes the chart lacks, and the chart's sizes no variant
  has (case and spaces ignored, as `size_run` matches them);
- `findings(marketplace, category, inputs)`: the approval findings. A missing `required` view is
  an ERROR, a missing `recommended` one a WARNING, an `optional` one nothing. Where the size chart
  is required, the seller's saved chart is what counts (a photo alone is a suggestion source, never
  a fact): none saved is an ERROR, a variant size the chart lacks an ERROR, a chart size no variant
  has a WARNING. A variant has no size chart of its own (it is the style's).

Every finding is deterministic (invariant 2) and uses existing codes and paths, so no wire
contract changes: a missing view is `IMAGE_NON_COMPLIANT` at `listing` with
`raw_code=photo_view:<view>` (one issue per view), a size mismatch `CROSS_FIELD_MISMATCH` at
`facts.size_measurements`. Neither opens a seller question (the photos card and the size chart
card are where the seller acts). Nothing here runs while `PHOTO_VIEWS_ENABLED` is off.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from mkos_schemas.base import Slug, StrictModel
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, ImageSlot, Marketplace, Severity
from mkos_schemas.errors import ErrorItem
from pydantic import Field, model_validator

from app.core.config import get_settings

__all__ = [
    "DATA_PATH",
    "MergedView",
    "PhotoInput",
    "SizeChartCheck",
    "ViewInputs",
    "ViewRule",
    "ViewState",
    "checklist",
    "enabled",
    "findings",
    "inputs_of",
    "listing_findings",
    "load_inputs",
    "merged",
    "product_states",
    "saved_chart_sizes",
    "size_chart_check",
    "size_chart_required",
    "size_flags",
    "variant_sizes",
    "view_of",
    "view_status",
]

PhotoView = Literal["front", "back", "close_up", "size_chart", "lifestyle", "packaging_label"]
Requirement = Literal["required", "recommended", "optional"]
ViewStatus = Literal["present", "missing", "invalid"]

DATA_PATH: Final = Path(__file__).resolve().parent / "data" / "photo_views.json"
VIEW_ORDER: Final[tuple[PhotoView, ...]] = (
    "front",
    "back",
    "close_up",
    "size_chart",
    "lifestyle",
    "packaging_label",
)
VIEW_SLOTS: Final[Mapping[PhotoView, tuple[ImageSlot, str | None]]] = {
    "front": (ImageSlot.MAIN, None),
    "back": (ImageSlot.SECONDARY, "back"),
    "close_up": (ImageSlot.SECONDARY, "close_up"),
    "size_chart": (ImageSlot.SIZE_CHART, None),
    "lifestyle": (ImageSlot.LIFESTYLE, None),
    "packaging_label": (ImageSlot.PACKAGING_LABEL, None),
}
VIEW_LABELS: Final[Mapping[PhotoView, str]] = {
    "front": "Front",
    "back": "Back",
    "close_up": "Close-up",
    "size_chart": "Size chart",
    "lifestyle": "Lifestyle",
    "packaging_label": "Packaging label",
}
RANK: Final[Mapping[Requirement, int]] = {"optional": 0, "recommended": 1, "required": 2}
SIZE_CHART_FIELD: Final = "facts.size_measurements"
SIZE_CHART_KEY: Final = "size_measurements"
VIEW_RAW_PREFIX: Final = "photo_view:"
_MISSING_MESSAGE: Final[Mapping[PhotoView, str]] = {
    "front": "Add a front photo.",
    "back": "Add a back photo.",
    "close_up": "Add a close-up photo.",
    "size_chart": "Add the size chart.",
    "lifestyle": "Add a lifestyle photo.",
    "packaging_label": "Add a photo of the packaging label.",
}


class _ViewIn(StrictModel):
    view: PhotoView
    requirement: Requirement
    why: Annotated[str, Field(min_length=1, max_length=160)]


class _ChecklistIn(StrictModel):
    marketplace: Marketplace | None = None
    category: Slug | None = None
    views: Annotated[list[_ViewIn], Field(min_length=1, max_length=len(VIEW_ORDER))]

    @model_validator(mode="after")
    def _views(self) -> _ChecklistIn:
        names = [v.view for v in self.views]
        if len(set(names)) != len(names):
            raise ValueError("a view is listed twice")
        front = next((v for v in self.views if v.view == "front"), None)
        if front is None or front.requirement != "required":
            raise ValueError("every checklist requires the front photo")
        return self


class _DataIn(StrictModel):
    version: Annotated[int, Field(ge=1)]
    checklists: Annotated[list[_ChecklistIn], Field(min_length=1)]

    @model_validator(mode="after")
    def _keys(self) -> _DataIn:
        keys = [(c.marketplace, c.category) for c in self.checklists]
        if len(set(keys)) != len(keys):
            raise ValueError("a (marketplace, category) checklist is listed twice")
        if (None, None) not in keys:
            raise ValueError("the default checklist (no marketplace, no category) is missing")
        return self


@dataclass(frozen=True, slots=True)
class ViewRule:
    view: PhotoView
    requirement: Requirement
    why: str


@dataclass(frozen=True, slots=True)
class MergedView:
    """One view across the marketplaces: the strictest requirement, each marketplace's own (a
    marketplace whose list leaves the view out is absent), and the why of the strictest."""

    view: PhotoView
    requirement: Requirement
    why: str
    by_marketplace: Mapping[Marketplace, Requirement]


@dataclass(frozen=True, slots=True)
class PhotoInput:
    image_id: UUID
    slot: str
    role: str | None
    compliance_status: str


@dataclass(frozen=True, slots=True)
class ViewInputs:
    """What the checks read for one product: its live photos, the seller's saved chart sizes
    (None when no chart is saved) and its variants' sizes (a style's; empty otherwise)."""

    images: tuple[PhotoInput, ...] = ()
    chart_sizes: tuple[str, ...] | None = None
    variant_sizes: tuple[str, ...] = ()
    is_variant: bool = False


Key = tuple[Marketplace | None, str | None]


def parse(raw: Mapping[str, Any]) -> Mapping[Key, tuple[ViewRule, ...]]:
    """The checklists by (marketplace, category), each in `VIEW_ORDER`; a bad file raises."""
    data = _DataIn.model_validate(raw)
    out: dict[Key, tuple[ViewRule, ...]] = {}
    for entry in data.checklists:
        rules = {v.view: ViewRule(v.view, v.requirement, v.why) for v in entry.views}
        out[(entry.marketplace, entry.category)] = tuple(
            rules[name] for name in VIEW_ORDER if name in rules
        )
    return out


@lru_cache(maxsize=1)
def _data() -> Mapping[Key, tuple[ViewRule, ...]]:
    return parse(json.loads(DATA_PATH.read_text(encoding="utf-8")))


def checklist(
    marketplace: Marketplace,
    category: str | None,
    data: Mapping[Key, tuple[ViewRule, ...]] | None = None,
) -> tuple[ViewRule, ...]:
    table = _data() if data is None else data
    for key in ((marketplace, category), (None, category), (marketplace, None), (None, None)):
        if key in table:
            return table[key]
    return ()  # unreachable: `parse` refuses a file without the (any, any) default


def merged(
    category: str | None, data: Mapping[Key, tuple[ViewRule, ...]] | None = None
) -> tuple[MergedView, ...]:
    per: dict[PhotoView, dict[Marketplace, ViewRule]] = {}
    for marketplace in Marketplace:
        for rule in checklist(marketplace, category, data):
            per.setdefault(rule.view, {})[marketplace] = rule
    out: list[MergedView] = []
    for name in VIEW_ORDER:
        rules = per.get(name)
        if not rules:
            continue
        strictest = max(rules.values(), key=lambda r: RANK[r.requirement])
        out.append(
            MergedView(
                view=name,
                requirement=strictest.requirement,
                why=strictest.why,
                by_marketplace={m: r.requirement for m, r in rules.items()},
            )
        )
    return tuple(out)


def size_chart_required(
    marketplace: Marketplace,
    category: str | None,
    data: Mapping[Key, tuple[ViewRule, ...]] | None = None,
) -> bool:
    return any(
        r.view == "size_chart" and r.requirement == "required"
        for r in checklist(marketplace, category, data)
    )


def view_of(slot: str, role: str | None) -> PhotoView | None:
    """The view a stored photo fills; None for a plain `secondary` (an extra photo)."""
    for name, (view_slot, view_role) in VIEW_SLOTS.items():
        if slot == view_slot.value and (view_slot is not ImageSlot.SECONDARY or role == view_role):
            return name
    return None


def photos_of(view: PhotoView, images: Iterable[PhotoInput]) -> list[PhotoInput]:
    return [i for i in images if view_of(i.slot, i.role) == view]


def view_status(view: PhotoView, inputs: ViewInputs) -> ViewStatus:
    if view == "size_chart" and inputs.chart_sizes is not None:
        return "present"
    mine = photos_of(view, inputs.images)
    if not mine:
        return "missing"
    if all(i.compliance_status == "failed" for i in mine):
        return "invalid"
    return "present"


def fold(size: str) -> str:
    """`size_run`'s match: spaces collapsed, case folded."""
    return " ".join(size.split()).casefold()


def size_flags(chart: Sequence[str], sizes: Sequence[str]) -> tuple[list[str], list[str]]:
    """(variant sizes the chart lacks, chart sizes no variant has), each in its input order and
    spelling, without repeats."""
    in_chart = {fold(s) for s in chart}
    in_sizes = {fold(s) for s in sizes}
    lacking: list[str] = []
    for size in sizes:
        if fold(size) not in in_chart and fold(size) not in {fold(x) for x in lacking}:
            lacking.append(size)
    unused: list[str] = []
    for size in chart:
        if fold(size) not in in_sizes and fold(size) not in {fold(x) for x in unused}:
            unused.append(size)
    return lacking, unused


def saved_chart_sizes(attributes: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    """The size labels of the chart the seller saved (`source: seller`, as `size_run` reads it),
    or None when none is saved."""
    fact = (attributes or {}).get(SIZE_CHART_KEY)
    if not isinstance(fact, Mapping) or fact.get("source") != "seller":
        return None
    table = fact.get("normalized")
    rows = table.get("rows") if isinstance(table, Mapping) else None
    if not isinstance(rows, list) or not rows:
        return None
    return tuple(
        r["size"] for r in rows if isinstance(r, Mapping) and isinstance(r.get("size"), str)
    )


def _item(
    marketplace: Marketplace,
    *,
    field: str,
    code: ErrorCode,
    severity: Severity,
    message: str,
    raw_code: str,
    suggestion: str | None = None,
) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=code,
        severity=severity,
        message=message,
        suggestion=suggestion,
        source=ErrorSource.RULES_ENGINE,
        retryable=False,
        marketplace=marketplace,
        raw_code=raw_code,
    )


def _sizes_text(sizes: Sequence[str]) -> str:
    return ", ".join(sizes[:12]) + (" and more" if len(sizes) > 12 else "")


def findings(
    marketplace: Marketplace,
    category: str | None,
    inputs: ViewInputs,
    data: Mapping[Key, tuple[ViewRule, ...]] | None = None,
) -> tuple[ErrorItem, ...]:
    out: list[ErrorItem] = []
    for rule in checklist(marketplace, category, data):
        if rule.requirement == "optional":
            continue
        if rule.view == "size_chart":
            if not inputs.is_variant:
                out.extend(_size_chart_items(marketplace, rule, inputs))
            continue
        if view_status(rule.view, inputs) != "missing":
            continue  # a failed photo already blocks through its own check (the image gate)
        severity = Severity.ERROR if rule.requirement == "required" else Severity.WARNING
        out.append(
            _item(
                marketplace,
                field="listing",
                code=ErrorCode.IMAGE_NON_COMPLIANT,
                severity=severity,
                message=_MISSING_MESSAGE[rule.view],
                raw_code=VIEW_RAW_PREFIX + rule.view,
                suggestion=rule.why,
            )
        )
    return tuple(out)


def _size_chart_items(
    marketplace: Marketplace, rule: ViewRule, inputs: ViewInputs
) -> list[ErrorItem]:
    if inputs.chart_sizes is None:
        has_photo = bool(photos_of("size_chart", inputs.images))
        if has_photo and rule.requirement != "required":
            return []  # a recommended chart: its photo is enough
        message = (
            "Save the size chart: check the sizes and the unit read from the photo, then save."
            if has_photo
            else "Add the size chart: type it in, or upload a photo of it."
        )
        severity = Severity.ERROR if rule.requirement == "required" else Severity.WARNING
        return [
            _item(
                marketplace,
                field="listing",
                code=ErrorCode.IMAGE_NON_COMPLIANT,
                severity=severity,
                message=message,
                raw_code=VIEW_RAW_PREFIX + "size_chart",
                suggestion=rule.why,
            )
        ]
    lacking, unused = size_flags(inputs.chart_sizes, inputs.variant_sizes)
    out: list[ErrorItem] = []
    if lacking:
        out.append(
            _item(
                marketplace,
                field=SIZE_CHART_FIELD,
                code=ErrorCode.CROSS_FIELD_MISMATCH,
                severity=Severity.ERROR if rule.requirement == "required" else Severity.WARNING,
                message=f"The size chart has no row for: {_sizes_text(lacking)}.",
                raw_code="size_chart:size_missing",
                suggestion="Add these sizes to the size chart, or fix the size of the variant.",
            )
        )
    if unused:
        out.append(
            _item(
                marketplace,
                field=SIZE_CHART_FIELD,
                code=ErrorCode.CROSS_FIELD_MISMATCH,
                severity=Severity.WARNING,
                message=f"No variant has these chart sizes: {_sizes_text(unused)}.",
                raw_code="size_chart:size_unused",
                suggestion="Make the size variants, or remove the rows you do not sell.",
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class ViewState:
    """One row of a product's checklist: the merged rule, its status and the photos in it."""

    rule: MergedView
    status: ViewStatus
    image_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SizeChartCheck:
    """A style's or standalone's size chart, for the "Needed" mark and the size flags."""

    required: bool
    saved: bool
    missing_sizes: tuple[str, ...]
    unused_sizes: tuple[str, ...]


def product_states(
    category: str | None,
    inputs: ViewInputs,
    data: Mapping[Key, tuple[ViewRule, ...]] | None = None,
) -> tuple[ViewState, ...]:
    """The product read's checklist (a variant has no size chart row: the chart is its style's)."""
    return tuple(
        ViewState(
            rule=rule,
            status=view_status(rule.view, inputs),
            image_ids=tuple(i.image_id for i in photos_of(rule.view, inputs.images)),
        )
        for rule in merged(category, data)
        if not (inputs.is_variant and rule.view == "size_chart")
    )


def size_chart_check(
    category: str | None,
    inputs: ViewInputs,
    data: Mapping[Key, tuple[ViewRule, ...]] | None = None,
) -> SizeChartCheck | None:
    """None on a variant. `required` when any marketplace's checklist requires the chart."""
    if inputs.is_variant:
        return None
    required = any(size_chart_required(m, category, data) for m in Marketplace)
    if inputs.chart_sizes is None:
        return SizeChartCheck(required=required, saved=False, missing_sizes=(), unused_sizes=())
    lacking, unused = size_flags(inputs.chart_sizes, inputs.variant_sizes)
    return SizeChartCheck(
        required=required, saved=True, missing_sizes=tuple(lacking), unused_sizes=tuple(unused)
    )


def enabled() -> bool:
    return get_settings().photo_views_enabled


def inputs_of(row: Any, details: Iterable[Any], sizes: Sequence[str]) -> ViewInputs:
    """The inputs from a product row, its image details and its variants' sizes."""
    is_variant = row.parent_product_id is not None
    return ViewInputs(
        images=tuple(
            PhotoInput(
                image_id=d.image_id,
                slot=d.slot,
                role=d.role,
                compliance_status=d.compliance_status,
            )
            for d in details
        ),
        chart_sizes=None if is_variant else saved_chart_sizes(row.attributes),
        variant_sizes=tuple(sizes),
        is_variant=is_variant,
    )


async def variant_sizes(uow: Any, row: Any) -> list[str]:
    """A style's live variants' sizes; `[]` for a standalone or a variant."""
    if row.parent_product_id is not None or row.variation_theme is None:
        return []
    found: list[str] = await uow.products.child_sizes(row.id)
    return found


async def load_inputs(uow: Any, product_id: UUID) -> ViewInputs:
    """The product's live photos (a style's colourway photos included), the seller's saved chart
    and, for a style, its live variants' sizes."""
    product = await uow.products.get_by_id(product_id)
    if product is None:
        return ViewInputs()
    details = await uow.images.details_for_product(product_id)
    return inputs_of(product, details, await variant_sizes(uow, product))


async def listing_findings(uow: Any, listing: Any) -> tuple[ErrorItem, ...]:
    """The findings for a canonical listing's product, marketplace and category; `()` while the
    switch is off."""
    if not enabled():
        return ()
    inputs = await load_inputs(uow, listing.product_id)
    return findings(listing.marketplace, listing.category, inputs)
