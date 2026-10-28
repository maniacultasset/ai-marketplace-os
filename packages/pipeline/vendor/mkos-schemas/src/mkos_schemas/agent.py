from datetime import datetime
from uuid import UUID

from pydantic import Field

from mkos_schemas._base import WireModel
from mkos_schemas.enums import AgentName, AgentTrigger, BlockAgent, BlockStatus, RunStatus
from mkos_schemas.errors import ErrorReport


class TokenCounts(WireModel):
    input: int = 0
    output: int = 0
    cached_input: int = 0


class AgentRunRecord(WireModel):
    id: UUID
    tenant_id: UUID
    request_id: str = Field(min_length=1, max_length=128)
    agent_name: AgentName
    trigger: AgentTrigger
    attempt: int = Field(ge=1, le=3)
    tokens: TokenCounts = Field(default_factory=TokenCounts)
    cost_usd: str = Field(pattern=r"^-?\d+(\.\d{1,6})?$")
    duration_ms: int = Field(default=0, ge=0)
    status: RunStatus
    started_at: datetime
    created_at: datetime
    product_id: UUID | None = None
    listing_id: UUID | None = None
    job_id: UUID | None = None
    model_used: str | None = None
    prompt_version: str | None = None
    error_report: ErrorReport | None = None
    input_digest: str | None = None
    finished_at: datetime | None = None


class AgentBlockStatus(WireModel):
    agent: BlockAgent
    status: BlockStatus
    agent_run_id: UUID | None = None
    error: ErrorReport | None = None
