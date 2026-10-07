"""The `agent_runs` writer (S11 hand-off: S16 brings the first model call; invariant 7).

`record_agent_run(record)` writes one row in its own short `tenant_uow(record.tenant_id)` with no
user, so the row survives whatever the caller's own transaction does afterwards (a call that
failed is still on record) and no caller holds a transaction open across a model call. A replay
of the same record id writes nothing (`ON CONFLICT (id) DO NOTHING`)."""

from __future__ import annotations

from mkos_schemas.agent import AgentRunRecord

from app.repositories.uow import tenant_uow


async def record_agent_run(record: AgentRunRecord) -> None:
    async with tenant_uow(record.tenant_id) as uow:
        await uow.agent_runs.insert(record)
