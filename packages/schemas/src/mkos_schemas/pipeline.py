"""AI seam protocol: PipelineDeps, run_listing_pipeline, edit_image (SI7), read_size_chart (SI12),
classify_product and map_columns (SI14)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol
from uuid import UUID

from .agent import AgentRunRecord
from .classify import ClassifyRequest, ClassifyResult
from .columns import ColumnMapRequest, ColumnMapResult
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
from .photo_facts_read import PhotoFactsReadRequest, PhotoFactsReadResult
from .rules import Rule, RuleSet
from .size_chart_read import SizeChartReadRequest, SizeChartReadResult

__all__ = [
    "AgentBlockStatus",
    "AgentRunRecord",
    "BrandChunkDTO",
    "CategoryTemplate",
    "ClassifyProductFn",
    "ClassifyRequest",
    "ClassifyResult",
    "ColumnMapRequest",
    "ColumnMapResult",
    "EditImage",
    "GenerationContext",
    "GenerationResult",
    "ImageEditRequest",
    "ImageEditResult",
    "MapColumnsFn",
    "ModelCredentials",
    "PhotoFactsReadRequest",
    "PhotoFactsReadResult",
    "PipelineDeps",
    "ReadPhotoFacts",
    "ReadSizeChart",
    "Rule",
    "RuleSet",
    "SizeChartReadRequest",
    "SizeChartReadResult",
    "edit_image",
    "read_size_chart",
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


class ReadSizeChart(Protocol):
    """The third AI-seam entry point (SI12): one size-chart read, called only by the backend worker."""

    async def __call__(
        self, request: SizeChartReadRequest, credentials: ModelCredentials
    ) -> SizeChartReadResult: ...


async def read_size_chart(
    request: SizeChartReadRequest, credentials: ModelCredentials
) -> SizeChartReadResult:
    """Implemented in mkos_pipeline; this module declares the contract only."""
    raise NotImplementedError("read_size_chart is implemented in mkos_pipeline")


class ReadPhotoFacts(Protocol):
    """V1.1 (read the photos on upload): one photo-facts read outside a listing generation, called
    only by the backend worker. Optional in a pipeline pin: the backend looks it up lazily, and a
    pin without it reads the photos at generation only, as before."""

    async def __call__(
        self, request: PhotoFactsReadRequest, credentials: ModelCredentials
    ) -> PhotoFactsReadResult: ...


class ClassifyProductFn(Protocol):
    """The fourth AI-seam entry point (SI14): one product classification, called only by the
    backend worker. Optional in a pipeline pin: the backend looks it up lazily."""

    async def __call__(
        self, request: ClassifyRequest, credentials: ModelCredentials
    ) -> ClassifyResult: ...


class MapColumnsFn(Protocol):
    """The fifth AI-seam entry point (SI14): the leftover spreadsheet headers of one column map,
    called only by the backend worker. Optional in a pipeline pin, like `ClassifyProductFn`."""

    async def __call__(
        self, request: ColumnMapRequest, credentials: ModelCredentials
    ) -> ColumnMapResult: ...
