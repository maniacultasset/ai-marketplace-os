"""Canonical listing shapes (marketplace-neutral)."""

from __future__ import annotations

import re
from decimal import Decimal
from math import isfinite
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .base import SCHEMA_VERSION, AttrKey, NonBlankStr, Slug, WireModel
from .enums import ImageSlot, ListingStatus, Locale, Marketplace
from .paths import is_valid_field_path

MAX_LISTING_IMAGES: Final = 20
MAX_LISTING_ATTRIBUTES: Final = 128
MAX_LISTING_CLAIMS: Final = 64
MAX_ATTRIBUTE_SOURCE_LENGTH: Final = 192
MAX_KEYWORD_OR_ATTR_STRING: Final = 256
_CLAIM_TEXT_PATH: Final = re.compile(r"^content\.(title|description|bullets\[[0-9]{1,3}\])$")


def _max_code_points(value: str, limit: int) -> str:
    if len(value) > limit:
        raise ValueError(f"exceeds {limit} code points")
    return value


class ListingContent(WireModel):
    title: Annotated[NonBlankStr, Field(max_length=500)]
    bullets: Annotated[
        list[Annotated[NonBlankStr, Field(max_length=1000)]], Field(min_length=1, max_length=10)
    ]
    description: Annotated[NonBlankStr, Field(max_length=5000)]


class ListingKeywords(WireModel):
    search_terms: Annotated[list[NonBlankStr], Field(max_length=50)] = Field(default_factory=list)
    hidden_terms: Annotated[list[NonBlankStr], Field(max_length=50)] = Field(default_factory=list)

    @field_validator("search_terms", "hidden_terms")
    @classmethod
    def _term_length(cls, terms: list[str]) -> list[str]:
        for term in terms:
            if len(term) > MAX_KEYWORD_OR_ATTR_STRING:
                raise ValueError("term too long")
        return terms


class ImageRef(WireModel):
    image_id: UUID
    slot: ImageSlot
    position: Annotated[int, Field(ge=0, le=99)] = 0

    @field_validator("slot")
    @classmethod
    def _refuse_packaging_label(cls, slot: ImageSlot) -> ImageSlot:
        if slot is ImageSlot.PACKAGING_LABEL:
            raise ValueError("packaging_label is not publishable on CanonicalListing.images")
        return slot


AttributeValue = str | int | float | bool | list[str]


class Claim(WireModel):
    """A factual statement kept in the listing text, and the product facts behind it (1.2.0)."""

    id: UUID
    text_path: str  # content.title, content.bullets[<n>] or content.description
    fact_keys: list[str]  # dotted product paths that support the claim, e.g. attributes.color
    status: Literal["supported", "unsupported", "removed"]

    @field_validator("text_path")
    @classmethod
    def _text_path(cls, value: str) -> str:
        if not is_valid_field_path(value) or not _CLAIM_TEXT_PATH.fullmatch(value):
            raise ValueError(f"{value!r} is not a listing text path")
        return value


class CanonicalListing(WireModel):
    schema_version: str = SCHEMA_VERSION
    listing_id: UUID | None = None
    product_id: UUID
    marketplace: Marketplace
    category: Slug
    status: ListingStatus = ListingStatus.CREATED
    version: Annotated[int, Field(ge=1)] = 1
    locale: Locale = "en-IN"
    content: ListingContent
    keywords: ListingKeywords = Field(default_factory=ListingKeywords)
    attributes: dict[AttrKey, AttributeValue] = Field(default_factory=dict)
    images: list[ImageRef] = Field(default_factory=list)
    confidence_score: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None
    generated_by_agent_run_id: UUID | None = None
    created_at: AwareDatetime | None = None
    claims: Annotated[list[Claim], Field(max_length=MAX_LISTING_CLAIMS)] = Field(
        default_factory=list
    )  # 1.2.0
    # 1.2.0: attribute key -> the fact key or dotted product path its value came from.
    attribute_sources: Annotated[
        dict[AttrKey, Annotated[NonBlankStr, Field(max_length=MAX_ATTRIBUTE_SOURCE_LENGTH)]],
        Field(max_length=MAX_LISTING_ATTRIBUTES),
    ] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _attribute_count(cls, attrs: dict[str, AttributeValue]) -> dict[str, AttributeValue]:
        if len(attrs) > MAX_LISTING_ATTRIBUTES:
            raise ValueError("too many attribute keys")
        return attrs

    @field_validator("images")
    @classmethod
    def _image_count(cls, images: list[ImageRef]) -> list[ImageRef]:
        if len(images) > MAX_LISTING_IMAGES:
            raise ValueError("too many images")
        return images

    @model_validator(mode="after")
    def _attribute_values(self) -> CanonicalListing:
        for key, value in self.attributes.items():
            if isinstance(value, str) and len(value) > MAX_KEYWORD_OR_ATTR_STRING:
                raise ValueError(f"attribute {key} string too long")
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and len(item) > MAX_KEYWORD_OR_ATTR_STRING:
                        raise ValueError(f"attribute {key} list item too long")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError("non-finite float attribute")
            if isinstance(value, Decimal) and not value.is_finite():
                raise ValueError("non-finite decimal attribute")
            if isinstance(value, dict):
                raise ValueError("object attribute values forbidden")
        if self.confidence_score is not None and not isfinite(self.confidence_score):
            raise ValueError("confidence_score must be finite")
        return self
