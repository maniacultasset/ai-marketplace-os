from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, SecretStr

from mkos_schemas._base import WireModel
from mkos_schemas.enums import ChunkSourceType, ChunkType, ModelPurpose, NodeStatus, PipelineStage, Provider
from mkos_schemas.errors import ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import GenerationContext, GenerationResult
from mkos_schemas.listing import CanonicalListing


class ModelCredentials(WireModel):
    provider: Provider
    api_key: SecretStr
    model: str
    timeout_seconds: int = Field(default=60, ge=1)
    api_base: str | None = None


class BrandChunkDTO(WireModel):
    id: UUID
    tenant_id: UUID
    type: ChunkType
    text: str
    similarity: float
    source_type: ChunkSourceType
    source_id: UUID
    chunk_index: int


class PipelineDeps(Protocol):
    async def retrieve_brand_chunks(
        self,
        tenant_id: UUID,
        query: str,
        k: int = 8,
        types: list[ChunkType] | None = None,
    ) -> list[BrandChunkDTO]: ...

    async def validate_rules(self, listing: CanonicalListing) -> ErrorReport: ...

    async def validate_images(self, listing: CanonicalListing) -> list[ImageReport]: ...

    async def get_model_credentials(self, purpose: ModelPurpose) -> ModelCredentials: ...

    async def emit_node_event(
        self,
        node: PipelineStage,
        status: NodeStatus,
        detail: dict[str, Any] | None = None,
    ) -> None: ...


async def run_listing_pipeline(ctx: GenerationContext, deps: PipelineDeps) -> GenerationResult:
    """Signature the pipeline package must match. Implemented by mkos_pipeline.graph."""
    raise NotImplementedError
