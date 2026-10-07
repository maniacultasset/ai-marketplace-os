"""`agent_runs` (DATABASE.md:983-997): one row per model call (invariant 7), append-only, written
only by the backend. `insert` is a Core `INSERT ... ON CONFLICT (id) DO NOTHING`, so a replayed
record (same id) never writes a second row; `app_rw` has no UPDATE or DELETE on the table."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from mkos_schemas.agent import AgentRunRecord
from sqlalchemy import null
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.guard import TENANT_INFO_KEY, CrossTenantQueryError
from app.db.models.runs import AgentRun
from app.repositories.base import TenantScopedRepository


def _row(record: AgentRunRecord) -> dict[str, Any]:
    finished = None
    if record.status != "running":
        finished = record.started_at + timedelta(milliseconds=record.duration_ms)
    report = record.error_report
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "product_id": record.product_id,
        "listing_id": record.listing_id,
        "listing_version_id": record.listing_version_id,
        "job_id": record.job_id,
        "trigger": record.trigger,
        "model_used": record.model_used,
        "tokens_in": record.tokens.input,
        "tokens_out": record.tokens.output,
        "tokens_cached_in": record.tokens.cached_input,
        "duration_ms": record.duration_ms,
        "status": record.status,
        # SQL NULL, not JSON `null` (a JSONB bind of None is `'null'`, which the CHECK refuses)
        "error_report_json": null() if report is None else report.model_dump(mode="json"),
        "cost_usd": record.cost_usd,
        "request_id": record.request_id,
        "agent_name": record.agent_name,
        "attempt": record.attempt,
        "prompt_version": record.prompt_version,
        "started_at": record.started_at,
        "finished_at": finished,
        "input_digest": record.input_digest,
    }


class AgentRunRepository(TenantScopedRepository[AgentRun]):
    model = AgentRun

    async def insert(self, record: AgentRunRecord) -> None:
        if record.tenant_id != self.tenant_id:
            raise CrossTenantQueryError("agent_runs record for another tenant")
        if self.db.info.get(TENANT_INFO_KEY) != self.tenant_id:
            raise CrossTenantQueryError("agent_runs write for another tenant's unit of work")
        stmt = (
            pg_insert(AgentRun)
            .values(**_row(record))
            .on_conflict_do_nothing(index_elements=[AgentRun.id])
        )
        await self.db.execute(stmt)
