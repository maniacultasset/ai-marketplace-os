from typing import Literal

Marketplace = Literal["amazon", "flipkart"]
Locale = Literal["en-IN"]

PipelineStage = Literal[
    "load_context",
    "retrieve_brand",
    "content_agent",
    "keyword_agent",
    "attribute_agent",
    "image_agent",
    "merge_outputs",
    "validate_schema",
    "validate_rules",
    "validate_vision",
    "correct",
    "quality_check",
]

NodeStatus = Literal["started", "ok", "failed"]
ModelPurpose = Literal["generation", "vision", "embedding"]
Provider = Literal["openai", "deepseek"]

AgentName = Literal[
    "content",
    "keyword",
    "attribute",
    "image",
    "correction",
    "embedding",
    "quality",
]
AgentTrigger = Literal["generate", "correct", "embed", "vision"]
BlockAgent = Literal["content", "keyword", "attribute", "image"]
BlockStatus = Literal["ok", "failed", "skipped"]
RunStatus = Literal["running", "completed", "failed", "timeout"]
TerminalState = Literal["generated", "needs_review", "blocked", "failed"]

Severity = Literal["error", "warning"]
ErrorSource = Literal[
    "schema",
    "rules_engine",
    "vision_model",
    "ai_model",
    "amazon_adapter",
    "flipkart_adapter",
    "system",
]
ErrorClass = Literal[
    "missing_data",
    "invalid_value",
    "category_mismatch",
    "variation_error",
    "media_error",
    "content_error",
    "identifier_error",
    "compliance_error",
    "account_permission",
    "marketplace_technical",
    "unknown",
]
RetryClass = Literal["auto_retry", "ai_correct", "user_action", "system_failure"]
FixAction = Literal["remap_value", "reformat", "derive_field", "rebuild_variation"]
ReportStatus = Literal["passed", "failed", "partial"]

ImageSlot = Literal["main", "secondary", "lifestyle", "size_chart", "packaging_label"]
ListingStatus = Literal[
    "created",
    "generating",
    "validated",
    "needs_review",
    "approved",
    "publishing",
    "published",
    "blocked",
    "failed",
]
ChunkType = Literal["tone_rule", "glossary", "compliance", "example"]
ChunkSourceType = Literal["brand_document", "listing_version"]
