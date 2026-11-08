"""The platform admin's tenant list and trial recovery (SI5, ADR-0029 §5).

The admin is a normal principal whose own `users.is_platform_admin` is true; the router checks it
on every request (`require_platform_admin`), and the database checks it again: the list reads
through `tenants_platform_admin` and the moves run `activate_tenant` / `extend_tenant_trial`
(0017), which raise GRD01 for anyone else. A move is audited as `staff` with the admin's email.
- `activate`: trial or suspended -> active; active answers the row unchanged; offboarded is 409.
- `extend`: a trial ends `days` later; a tenant suspended by trial expiry returns to trial; any
  other state is 409. Suspend and offboard stay with the staff CLI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.repositories.identity import TenantAdminRepository, TenantAdminRow
from app.repositories.transactions import identity_session, set_gucs

__all__ = ["TenantAdminRow", "TenantPage", "activate", "extend", "list_tenants"]


@dataclass(frozen=True, slots=True)
class TenantPage:
    items: list[TenantAdminRow]
    has_more: bool


async def list_tenants(
    principal: Principal,
    *,
    status: str | None,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
) -> TenantPage:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        rows = await TenantAdminRepository(session).page(
            status=status, limit=limit + 1, after=after
        )
    return TenantPage(items=rows[:limit], has_more=len(rows) > limit)


def _outcome(outcome: str) -> None:
    if outcome == "not_found":
        raise NotFoundError("target_tenant_id")
    if outcome == "refused":
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "tenants_status")


async def _reread(repo: TenantAdminRepository, tenant_id: uuid.UUID) -> TenantAdminRow:
    row = await repo.get(tenant_id)
    if row is None:  # pragma: no cover - the definer just found it
        raise NotFoundError("target_tenant_id")
    return row


async def activate(principal: Principal, tenant_id: uuid.UUID, *, reason: str) -> TenantAdminRow:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        repo = TenantAdminRepository(session)
        _outcome(await repo.activate(tenant_id, reason=reason))
        return await _reread(repo, tenant_id)


async def extend(
    principal: Principal, tenant_id: uuid.UUID, *, days: int, reason: str
) -> TenantAdminRow:
    async with identity_session() as session:
        await set_gucs(session, user_id=principal.user_id)
        repo = TenantAdminRepository(session)
        _outcome(await repo.extend_trial(tenant_id, days=days, reason=reason))
        return await _reread(repo, tenant_id)
