"""SI12: what each marketplace needs for a category (`GET /catalog/requirements`); client
feedback 11 Nov: the category picker (`GET /catalog/categories`) and the suggestion before save
(`POST /catalog/category-suggestions`, `GET /catalog/category-suggestions/{suggestion_id}`)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, Slug, StrictModel, WireModel
from mkos_schemas.enums import Marketplace
from pydantic import Field, StringConstraints


class RequirementsOut(WireModel):
    """Each list is the sorted dictionary keys the marketplace needs before it accepts a listing
    of this category: its required facts, the category template's required attributes and a
    reviewed extras table. Shown to the seller; the extras are never sent."""

    category: Slug
    amazon: list[str] = Field(default_factory=list)
    flipkart: list[str] = Field(default_factory=list)


# ---- client feedback 11 Nov (item 5): the category picker ----


class MarketplaceCategoryOut(WireModel):
    """What one marketplace is sent for a listing of the category."""

    marketplace: Marketplace
    product_type: Annotated[str, Field(max_length=200)]
    """The marketplace's own code, as the listing sends it (Amazon `KURTA`, Flipkart `kurta`)."""
    product_type_name: Annotated[str, Field(max_length=200)]
    """Its plain name ("Kurta")."""
    external_category_id: Annotated[str, Field(max_length=200)] | None = None
    """The marketplace's browse node or category id, once mapped (null until then)."""
    template_version: int


class CategoryOut(WireModel):
    category: Slug
    label: Annotated[str, Field(max_length=200)]
    enabled: bool
    """False while the category is switched off (lamps while `CATEGORY_LAMPS_ENABLED` is off):
    shown on an existing product, never offered for a new one."""
    marketplaces: list[MarketplaceCategoryOut] = Field(default_factory=list)


class CategoryCatalogOut(WireModel):
    """Every category with a current template, sorted by label, and whether the category
    suggestion is switched on (`CATEGORY_SUGGEST_ENABLED`)."""

    items: list[CategoryOut] = Field(default_factory=list)
    suggest_enabled: bool = False


# ---- client feedback 11 Nov (item 5): the suggestion before save ----

DraftText = Annotated[str, StringConstraints(max_length=200)]
DraftAttributeKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class CategoryDraftIn(StrictModel):
    """What the seller typed so far. Only the name is needed."""

    title: Annotated[NonBlankStr, Field(max_length=500)]
    description: Annotated[str, StringConstraints(max_length=5000)] | None = None
    brand: DraftText | None = None
    attributes: Annotated[dict[DraftAttributeKey, DraftText], Field(max_length=60)] = Field(
        default_factory=dict
    )


class CategoryDraftCandidateOut(WireModel):
    category: Slug
    label: Annotated[str, Field(max_length=200)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    rationale: Annotated[str, Field(max_length=300)]
    """One short line on why it fits."""
    unsure: bool
    """True below 0.80: show it as a guess, never as the answer."""


class CategoryDraftSuggestionOut(WireModel):
    """202 `queued` with the id to read; 200 `unavailable` while the suggestion is switched off
    (no id, nothing spent). The read answers `queued`, `suggested` (up to 3 candidates, highest
    confidence first; none when nothing fits), `unavailable` or `failed`."""

    suggestion_id: UUID | None = None
    status: Literal["queued", "suggested", "unavailable", "failed"]
    candidates: list[CategoryDraftCandidateOut] = Field(default_factory=list)
