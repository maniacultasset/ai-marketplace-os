"""AI Marketplace OS contract types (schemas 1.2.0 stand-in)."""

from mkos_schemas.agent import AgentBlockStatus, AgentRunRecord, TokenCounts
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import (
    AgentName,
    AgentTrigger,
    ImageSlot,
    Marketplace,
    PipelineStage,
    RunStatus,
    Severity,
    TerminalState,
)
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageCheck, ImageIssue, ImageRef, ImageReport
from mkos_schemas.jobs import GenerationContext, GenerationResult
from mkos_schemas.listing import CanonicalListing, ListingContent, ListingKeywords
from mkos_schemas.pipeline import ModelCredentials, PipelineDeps, run_listing_pipeline
from mkos_schemas.rules import AttributeSpec, CategoryTemplate, Rule, RuleSet
from mkos_schemas.validation import ValidationResult, ValidationSummary

SCHEMA_VERSION = "1.2.0"

__all__ = [
    "SCHEMA_VERSION",
    "AgentBlockStatus",
    "AgentName",
    "AgentRunRecord",
    "AgentTrigger",
    "AttributeSpec",
    "CanonicalListing",
    "CategoryTemplate",
    "ErrorCode",
    "ErrorItem",
    "ErrorReport",
    "GenerationContext",
    "GenerationResult",
    "ImageCheck",
    "ImageIssue",
    "ImageRef",
    "ImageReport",
    "ImageSlot",
    "ListingContent",
    "ListingKeywords",
    "Marketplace",
    "ModelCredentials",
    "PipelineDeps",
    "PipelineStage",
    "Rule",
    "RuleSet",
    "RunStatus",
    "Severity",
    "TerminalState",
    "TokenCounts",
    "ValidationResult",
    "ValidationSummary",
    "run_listing_pipeline",
]
