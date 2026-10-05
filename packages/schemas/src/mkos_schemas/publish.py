"""Marketplace publish results (per-field outcomes)."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, field_validator

from .base import SCHEMA_VERSION, NonBlankStr, WireModel
from .enums import ItemMarketplaceState, Marketplace, MarketplaceState
from .errors import ErrorItem, ErrorReport
from .paths import is_valid_field_path


class FieldResult(WireModel):
    field: str
    status: Literal["succeeded", "failed", "skipped"]
    error: ErrorItem | None = None

    @field_validator("field")
    @classmethod
    def _path(cls, value: str) -> str:
        if not is_valid_field_path(value):
            raise ValueError(f"{value!r} is not a legal field path")
        return value


class ItemResult(WireModel):
    product_id: UUID
    seller_sku: Annotated[NonBlankStr, Field(max_length=64)]
    external_id: str | None = None
    marketplace_state: ItemMarketplaceState
    live_url: Annotated[str | None, Field(default=None, max_length=2048)] = None
    fields: list[FieldResult] = Field(default_factory=list)

    @field_validator("live_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("live_url must be https")
        return value


class ListingResult(WireModel):
    schema_version: str = SCHEMA_VERSION
    listing_id: UUID
    marketplace: Marketplace
    external_id: str | None = None
    status: Literal["published", "partially_published", "failed"]
    idempotency_key: NonBlankStr
    fields: list[FieldResult] = Field(default_factory=list)
    submission_ref: Annotated[str | None, Field(default=None, max_length=128)] = None
    marketplace_state: MarketplaceState | None = None
    items: list[ItemResult] = Field(default_factory=list)
    raw_response_digest: str | None = None
    error: ErrorReport | None = None
    attempt: Annotated[int, Field(ge=1, le=3)] = 1
