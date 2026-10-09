"""Marketplace publish results (per-field outcomes) and the marketplace preview (what the adapter
would send, sending nothing)."""

from __future__ import annotations

from typing import Annotated, Literal, get_args
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

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


ItemRole = Literal["standalone", "parent", "child"]
ITEM_ROLES = frozenset(get_args(ItemRole))
_IMAGE_KEY = "media_location"


def _has_url_image(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(
            (key == _IMAGE_KEY and isinstance(v, str) and "://" in v) or _has_url_image(v)
            for key, v in value.items()
        )
    if isinstance(value, list):
        return any(_has_url_image(v) for v in value)
    return False


class PreviewItem(WireModel):
    """One item the adapter would send: the listing's own SKU (standalone or parent) or a child.
    `payload` is the marketplace body; images are opaque `image:<id>` references, never URLs."""

    product_id: UUID
    seller_sku: Annotated[NonBlankStr, Field(max_length=64)]
    item_role: ItemRole
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _no_image_urls(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if _has_url_image(value):
            raise ValueError("a preview image is an image:<id> reference, never a URL")
        return value


class MarketplacePreview(WireModel):
    """`GET /api/v1/listings/{listing_id}/preview`: the per-SKU payloads for the listing's current
    version and the adapter's mapping errors. It carries no verdict: the Rules Engine decides,
    the adapter only adds errors (`report`)."""

    schema_version: str = SCHEMA_VERSION
    listing_id: UUID
    version: Annotated[int, Field(ge=1)]
    marketplace: Marketplace
    product_type: Annotated[str | None, Field(default=None, max_length=128)] = None
    items: list[PreviewItem] = Field(default_factory=list)
    report: ErrorReport
