"""SI12: what each marketplace needs for a category (`GET /catalog/requirements`)."""

from __future__ import annotations

from typing import Literal

from mkos_schemas.base import Slug, WireModel
from pydantic import Field

RequirementLevel = Literal["required", "conditional", "recommended", "optional", "unsupported"]


class RequirementLevelsOut(WireModel):
    """Feedback item 10: each marketplace's current category template, as the requirement level
    of every attribute it names (`required`, `conditional`, `recommended`, `optional`,
    `unsupported`). Empty when the category has no template (or is switched off)."""

    amazon: dict[str, RequirementLevel] = Field(default_factory=dict)
    flipkart: dict[str, RequirementLevel] = Field(default_factory=dict)


class RequirementsOut(WireModel):
    """Each list is the sorted dictionary keys the marketplace needs before it accepts a listing
    of this category: its required facts, the category template's required attributes and a
    reviewed extras table. Shown to the seller; the extras are never sent. `levels` is the
    template's own requirement level per attribute."""

    category: Slug
    amazon: list[str] = Field(default_factory=list)
    flipkart: list[str] = Field(default_factory=list)
    levels: RequirementLevelsOut = Field(default_factory=RequirementLevelsOut)
