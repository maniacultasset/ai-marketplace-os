"""SI12: what each marketplace needs for a category (`GET /catalog/requirements`)."""

from __future__ import annotations

from mkos_schemas.base import Slug, WireModel
from pydantic import Field


class RequirementsOut(WireModel):
    """Each list is the sorted dictionary keys the marketplace needs before it accepts a listing
    of this category: its required facts, the category template's required attributes and a
    reviewed extras table. Shown to the seller; the extras are never sent."""

    category: Slug
    amazon: list[str] = Field(default_factory=list)
    flipkart: list[str] = Field(default_factory=list)
