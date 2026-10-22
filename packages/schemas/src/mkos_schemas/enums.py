"""Closed vocabularies shared across mkos-schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

Locale = Literal["en-IN"]


class Marketplace(StrEnum):
    AMAZON = "amazon"
    FLIPKART = "flipkart"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ErrorSource(StrEnum):
    SCHEMA = "schema"
    RULES_ENGINE = "rules_engine"
    VISION_MODEL = "vision_model"
    AI_MODEL = "ai_model"
    AMAZON_ADAPTER = "amazon_adapter"
    FLIPKART_ADAPTER = "flipkart_adapter"
    SYSTEM = "system"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class ListingStatus(StrEnum):
    CREATED = "created"
    GENERATING = "generating"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    BLOCKED = "blocked"
    FAILED = "failed"


class PipelineStage(StrEnum):
    """TRANSIENT progress only — never written to listings.status."""

    LOAD_CONTEXT = "load_context"
    RETRIEVE_BRAND = "retrieve_brand"
    CONTENT_AGENT = "content_agent"
    KEYWORD_AGENT = "keyword_agent"
    ATTRIBUTE_AGENT = "attribute_agent"
    IMAGE_AGENT = "image_agent"
    MERGE_OUTPUTS = "merge_outputs"
    VALIDATE_SCHEMA = "validate_schema"
    VALIDATE_RULES = "validate_rules"
    VALIDATE_VISION = "validate_vision"
    CORRECT = "correct"
    QUALITY_CHECK = "quality_check"


class ImageSlot(StrEnum):
    MAIN = "main"
    SECONDARY = "secondary"
    LIFESTYLE = "lifestyle"
    SIZE_CHART = "size_chart"
    PACKAGING_LABEL = "packaging_label"


PUBLISHABLE_SLOTS: Final[frozenset[ImageSlot]] = frozenset(
    s for s in ImageSlot if s is not ImageSlot.PACKAGING_LABEL
)


class ErrorClass(StrEnum):
    MISSING_DATA = "missing_data"
    INVALID_VALUE = "invalid_value"
    CATEGORY_MISMATCH = "category_mismatch"
    VARIATION_ERROR = "variation_error"
    MEDIA_ERROR = "media_error"
    CONTENT_ERROR = "content_error"
    IDENTIFIER_ERROR = "identifier_error"
    COMPLIANCE_ERROR = "compliance_error"
    ACCOUNT_PERMISSION = "account_permission"
    MARKETPLACE_TECHNICAL = "marketplace_technical"
    UNKNOWN = "unknown"


class RetryClass(StrEnum):
    AUTO_RETRY = "auto_retry"
    AI_CORRECT = "ai_correct"
    USER_ACTION = "user_action"
    SYSTEM_FAILURE = "system_failure"


class FixAction(StrEnum):
    REMAP_VALUE = "remap_value"
    REFORMAT = "reformat"
    DERIVE_FIELD = "derive_field"
    REBUILD_VARIATION = "rebuild_variation"


class Gate(StrEnum):
    """The nine quality gates before submission (spec L1388-1424); schemas 1.2.0."""

    IDENTITY = "identity"
    CLASSIFICATION = "classification"
    ATTRIBUTES = "attributes"
    VARIANTS = "variants"
    MEDIA = "media"
    CONTENT = "content"
    COMPLIANCE = "compliance"
    COMMERCIAL = "commercial"
    SUBMISSION = "submission"


class MarketplaceState(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    LIVE = "live"
    PARTIALLY_LIVE = "partially_live"
    INACTIVE = "inactive"
    REJECTED = "rejected"
    REMOVED = "removed"


class ItemMarketplaceState(StrEnum):
    NOT_SUBMITTED = "not_submitted"
    PROCESSING = "processing"
    PENDING_REVIEW = "pending_review"
    LIVE = "live"
    INACTIVE = "inactive"
    REJECTED = "rejected"
    REMOVED = "removed"


class JobKind(StrEnum):
    LISTING_GENERATE = "listing_generate"
    LISTING_PUBLISH = "listing_publish"
    LISTING_STATUS_SYNC = "listing_status_sync"
    BULK_IMPORT = "bulk_import"
    IMAGE_COMPLIANCE = "image_compliance"
    BRAND_EMBED = "brand_embed"
