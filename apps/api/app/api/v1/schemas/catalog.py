"""SI12: what each marketplace needs for a category (`GET /catalog/requirements`). Feedback 7:
the photo views the category's checklist asks for."""

from __future__ import annotations

from typing import Literal

from mkos_schemas.base import Slug, WireModel
from mkos_schemas.enums import ImageRole, ImageSlot
from pydantic import Field

PhotoViewName = Literal["front", "back", "close_up", "size_chart", "lifestyle", "packaging_label"]
PhotoViewRequirement = Literal["required", "recommended", "optional"]


class PhotoViewRuleOut(WireModel):
    """One photo view of a category's checklist. `requirement` is the strictest any marketplace
    gives it; `amazon` and `flipkart` are each marketplace's own (null: that marketplace's list
    leaves the view out). A photo fills the view when it is uploaded with this `slot` and
    `role`; the size chart also counts once the seller saved a chart."""

    view: PhotoViewName
    label: str
    slot: ImageSlot
    role: ImageRole | None = None
    requirement: PhotoViewRequirement
    why: str = Field(description="One plain line on why the view helps.")
    amazon: PhotoViewRequirement | None = None
    flipkart: PhotoViewRequirement | None = None


class RequirementsOut(WireModel):
    """Each list is the sorted dictionary keys the marketplace needs before it accepts a listing
    of this category: its required facts, the category template's required attributes and a
    reviewed extras table (with `PHOTO_VIEWS_ENABLED` on, `size_measurements` where the size
    chart is required). Shown to the seller; the extras are never sent."""

    category: Slug
    amazon: list[str] = Field(default_factory=list)
    flipkart: list[str] = Field(default_factory=list)
    photo_views: list[PhotoViewRuleOut] = Field(
        default_factory=list,
        description=(
            "Feedback 7: the category's photo view checklist, in display order; empty while "
            "the checklist is switched off (show the fixed Front, Back, Close-up and Size chart "
            "tiles then)."
        ),
    )
