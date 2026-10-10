"""Marketplace publish results (per-field outcomes), the marketplace preview (what the adapter
would send, sending nothing) and the submission ledger read (S21: one `SubmissionOut` per attempt,
with its per-item, per-field results; never a storage key, a digest or a raw marketplace body)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, get_args
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, JsonValue, field_validator

from .base import SCHEMA_VERSION, NonBlankStr, WireModel
from .codes import ErrorCode
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


SubmissionOutcome = Literal[
    "pending", "accepted", "partially_accepted", "rejected", "error", "live", "superseded"
]
SubmissionOperation = Literal["create", "update", "patch"]
RetryJustification = Literal[
    "changed_input", "retryable_condition", "marketplace_transition", "human_resolution"
]


class SubmissionItemOut(WireModel):
    """One marketplace SKU of a submission: its latest state and the field results this attempt
    recorded for it (empty while the attempt is `pending`)."""

    listing_item_id: UUID
    seller_sku: Annotated[NonBlankStr, Field(max_length=64)]
    marketplace_state: ItemMarketplaceState
    fields: list[FieldResult] = Field(default_factory=list)


class SubmissionOut(WireModel):
    """`GET /api/v1/listings/{listing_id}/submissions`: one publish attempt (at most 3 per
    approved version). `field_paths` is set only for a `patch` (the failed paths being resent);
    `error` is the attempt's one ErrorReport. Storage keys, digests and raw bodies stay internal."""

    id: UUID
    listing_version_id: UUID
    attempt: Annotated[int, Field(ge=1, le=3)]
    operation: SubmissionOperation
    field_paths: list[str] | None = None
    outcome: SubmissionOutcome
    error_code: ErrorCode | None = None
    retry_justification: RetryJustification | None = None
    requested_by_user_id: UUID
    items: list[SubmissionItemOut] = Field(default_factory=list)
    error: ErrorReport | None = None
    sent_at: datetime | None = None
    responded_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime

    @field_validator("field_paths")
    @classmethod
    def _paths(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not all(is_valid_field_path(p) for p in value):
            raise ValueError("field_paths must be legal field paths")
        return value
