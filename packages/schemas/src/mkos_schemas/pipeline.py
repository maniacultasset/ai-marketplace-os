"""AI seam protocol: PipelineDeps, run_listing_pipeline and the edit_image entry point (SI7)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from .agent import AgentRunRecord
from .enums import PipelineStage
from .errors import ErrorReport
from .image_edit import ImageEditRequest, ImageEditResult
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
    "AgentBlockStatus",
    "AgentRunRecord",
    "BrandChunkDTO",
    "CategoryTemplate",
    "EditImage",
    "GenerationContext",
    "GenerationResult",
    "ImageEditRequest",
    "ImageEditResult",
    "ModelCredentials",
    "PipelineDeps",
    "Rule",
    "RuleSet",
    "edit_image",
    "run_listing_pipeline",
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


async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult:
    """Implemented in mkos_pipeline.graph; this module declares the contract only."""
    raise NotImplementedError("run_listing_pipeline is implemented in mkos_pipeline.graph")


class EditImage(Protocol):
    """The second AI-seam entry point (SI7): one image edit, called only by the backend worker."""

    async def __call__(
        self, request: ImageEditRequest, credentials: ModelCredentials
    ) -> ImageEditResult: ...


async def edit_image(request: ImageEditRequest, credentials: ModelCredentials) -> ImageEditResult:
    """Implemented in mkos_pipeline (0.4.0); this module declares the contract only."""
    raise NotImplementedError("edit_image is implemented in mkos_pipeline")
