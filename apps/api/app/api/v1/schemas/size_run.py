"""V1.1: the size run from the saved size chart (`POST /products/{product_id}/size-run`)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from pydantic import Field, StringConstraints

SizeLabel = Annotated[str, StringConstraints(min_length=1, max_length=20, strip_whitespace=False)]


class SizeRunIn(StrictModel):
    """The sizes to make, each a row of the saved size chart (matched case-insensitively)."""

    sizes: Annotated[list[SizeLabel], Field(min_length=1, max_length=40)]


class SizeRunVariantOut(WireModel):
    id: UUID
    sku: str = Field(description="`<style sku>-<SIZE>`.")
    size: str = Field(description="The size as the chart spells it.")


class SizeRunOut(WireModel):
    """201 with the variants made; 200 with `created: []` when every size already exists."""

    product_id: UUID
    variation_theme: Literal["size"]
    size_run: list[str] = Field(description="The style's size keys, in order.")
    created: list[SizeRunVariantOut]
