from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.errors import ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import GenerationContext
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.pipeline import BrandChunkDTO, PipelineDeps

from mkos_pipeline.schema_compat import AgentBlockStatus
from mkos_pipeline.schemas_local import AttributeDraft, ContentDraft, KeywordDraft


@dataclass
class GraphState:
    ctx: GenerationContext
    deps: PipelineDeps
    deadline_at: datetime
    records: list[AgentRunRecord] = field(default_factory=list)
    blocks: list[AgentBlockStatus] = field(default_factory=list)
    chunks_by_facet: dict[str, list[BrandChunkDTO]] = field(default_factory=dict)
    draft: CanonicalListing | None = None
    attempts_used: int = 0
    last_report: ErrorReport | None = None
    content_draft: ContentDraft | None = None
    keyword_draft: KeywordDraft | None = None
    attribute_draft: AttributeDraft | None = None
    dropped_attributes: list[str] = field(default_factory=list)
    image_reports: list[ImageReport] = field(default_factory=list)
    vision_ran: bool = False
    locale_ok: bool = True
    terminal_state: str = "failed"
    extra: dict[str, Any] = field(default_factory=dict)

    def upsert_block(self, block: AgentBlockStatus) -> None:
        self.blocks = [item for item in self.blocks if item.agent != block.agent]
        self.blocks.append(block)

    def block_for(self, agent: str) -> AgentBlockStatus | None:
        for block in self.blocks:
            if block.agent == agent:
                return block
        return None
