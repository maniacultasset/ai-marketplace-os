"""Field requirements of one listing (client feedback item 16): `services/field_requirements.py`."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mkos_schemas.base import AttrKey, Slug, WireModel
from mkos_schemas.enums import Marketplace
from pydantic import Field

Count = Annotated[int, Field(ge=0)]
Limit = Annotated[int, Field(gt=0)]


class FieldRequirementOut(WireModel):
    path: Literal[
        "content.title",
        "content.bullets",
        "content.bullets[*]",
        "content.description",
        "keywords.search_terms",
        "keywords.hidden_terms",
        "*",
    ] = Field(description="The field; `content.bullets[*]` is each bullet, `*` every text field.")
    max_chars: Limit | None = None
    max_bytes: Limit | None = Field(
        default=None, description="UTF-8 bytes (Amazon backend keywords)."
    )
    max_words: Limit | None = None
    min_chars: Limit | None = None
    min_bytes: Limit | None = None
    min_words: Limit | None = None
    max_items: Limit | None = None
    min_items: Limit | None = None
    banned_terms: list[str] = Field(
        default_factory=list, description="The first 60 banned words or phrases."
    )
    banned_count: Count = Field(default=0, description="Every banned word or phrase on the field.")
    rules: list[str] = Field(
        default_factory=list,
        description="Plain lines for the other rules: claims that need an attribute, formats, "
        "patterns that are not allowed.",
    )


class AttributeRequirementOut(WireModel):
    key: AttrKey
    level: Literal["required", "conditional", "recommended", "optional", "unsupported"]
    value_type: Literal["text", "enum", "number", "boolean", "measurement"] | None = None
    allowed_values: list[str] = Field(
        default_factory=list, description="The first 100 allowed values; empty means any value."
    )
    allowed_count: Count = 0
    max_length: Limit | None = Field(default=None, description="Characters.")
    unit: str | None = Field(
        default=None, description="A measurement's unit, as the template names it."
    )


class ListingRequirementsOut(WireModel):
    listing_id: UUID
    marketplace: Marketplace
    category: Slug
    rules_version: Count
    has_template: bool = Field(description="False when the category has no current template.")
    fields: list[FieldRequirementOut] = Field(default_factory=list)
    attributes: list[AttributeRequirementOut] = Field(default_factory=list)
