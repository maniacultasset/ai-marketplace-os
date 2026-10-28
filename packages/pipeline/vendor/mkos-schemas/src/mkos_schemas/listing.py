from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints

from mkos_schemas._base import WireModel
from mkos_schemas.enums import ImageSlot, ListingStatus, Locale, Marketplace

AttributeKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
CategorySlug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{2,64}$")]
AttributeValue = str | int | float | bool | list[str]


class ListingContent(WireModel):
    title: str = Field(min_length=1, max_length=500)
    bullets: list[str] = Field(min_length=1, max_length=10)
    description: str = Field(min_length=1, max_length=5000)


class ListingKeywords(WireModel):
    search_terms: list[str] = Field(default_factory=list, max_length=50)
    hidden_terms: list[str] = Field(default_factory=list, max_length=50)


class ImageRef(WireModel):
    image_id: UUID
    slot: ImageSlot
    position: int = Field(default=0, ge=0, le=99)


class CanonicalListing(WireModel):
    product_id: UUID
    marketplace: Marketplace
    category: CategorySlug
    content: ListingContent
    listing_id: UUID | None = None
    locale: Locale = "en-IN"
    schema_version: str = "1.2.0"
    keywords: ListingKeywords = Field(default_factory=ListingKeywords)
    attributes: dict[AttributeKey, AttributeValue] = Field(default_factory=dict, max_length=128)
    images: list[ImageRef] = Field(default_factory=list, max_length=20)
    confidence_score: float | None = Field(default=None, ge=0, le=1)
    generated_by_agent_run_id: UUID | None = None
    status: ListingStatus = "created"
    version: int = Field(default=1, ge=1)
    created_at: datetime | None = None
