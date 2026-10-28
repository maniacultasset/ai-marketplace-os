from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from mkos_schemas.agent import AgentRunRecord, TokenCounts
from mkos_schemas.enums import AgentName, AgentTrigger, RunStatus
from mkos_schemas.errors import ErrorReport
from mkos_schemas.jobs import GenerationContext


def digest_prompt(rendered: str) -> str:
    return sha256(rendered.encode("utf-8")).hexdigest()


def append_record(
    records: list[AgentRunRecord],
    *,
    ctx: GenerationContext,
    agent_name: AgentName,
    trigger: AgentTrigger,
    attempt: int,
    status: RunStatus,
    cost_usd: str,
    started_at: datetime,
    duration_ms: int,
    model_used: str | None,
    prompt_version: str | None,
    input_digest: str,
    tokens: TokenCounts | None = None,
    error_report: ErrorReport | None = None,
) -> AgentRunRecord:
    now = datetime.now(UTC)
    record = AgentRunRecord(
        id=uuid4(),
        tenant_id=ctx.tenant_id,
        request_id=ctx.request_id,
        agent_name=agent_name,
        trigger=trigger,
        attempt=attempt,
        tokens=tokens or TokenCounts(),
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        status=status,
        started_at=started_at,
        created_at=now,
        product_id=ctx.product_id,
        listing_id=ctx.listing_id,
        job_id=ctx.job_id,
        model_used=model_used,
        prompt_version=prompt_version,
        error_report=error_report,
        input_digest=input_digest,
        finished_at=now,
    )
    records.append(record)
    return record


def job_ids(ctx: GenerationContext) -> tuple[UUID, UUID, UUID, str]:
    return ctx.tenant_id, ctx.product_id, ctx.listing_id, ctx.request_id
