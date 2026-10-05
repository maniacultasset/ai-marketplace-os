"""AI seam protocol: PipelineDeps and run_listing_pipeline entry point."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from .agent import AgentRunRecord
from .enums import PipelineStage
from .errors import ErrorReport
from .images import ImageReport
from .jobs import (
    AgentBlockStatus,
    BrandChunkDTO,
    CategoryTemplate,
    GenerationContext,
    GenerationResult,
    ModelCredentials,
)
from .listing import CanonicalListing
from .rules import Rule, RuleSet

__all__ = [
    "PipelineDeps",
    "run_listing_pipeline",
    "GenerationContext",
    "GenerationResult",
    "AgentBlockStatus",
    "AgentRunRecord",
    "BrandChunkDTO",
    "CategoryTemplate",
    "ModelCredentials",
    "Rule",
    "RuleSet",
]


class PipelineDeps(Protocol):
    async def retrieve_brand_chunks(
        self,
        tenant_id: UUID,
        query: str,
        k: int = 8,
        types: Sequence[str] | None = None,
    ) -> Sequence[BrandChunkDTO]: ...

    async def validate_rules(self, listing: CanonicalListing) -> ErrorReport: ...

    async def validate_images(self, listing: CanonicalListing) -> list[ImageReport]: ...

    async def get_model_credentials(
        self, purpose: Literal["generation", "vision", "embedding"]
    ) -> ModelCredentials: ...

    async def emit_node_event(
        self,
        node: PipelineStage,
        status: Literal["started", "ok", "failed"],
        detail: dict[str, Any] | None = None,
    ) -> None: ...


async def run_listing_pipeline(
    ctx: GenerationContext, deps: PipelineDeps
) -> GenerationResult:
    """Implemented in mkos_pipeline.graph; this module declares the contract only."""
    raise NotImplementedError("run_listing_pipeline is implemented in mkos_pipeline.graph")
