"""`GET /api/v1/rules` (S14b T11; the S3 `rules_list` registry row): the platform rules and the caller's own
brand rules, keyset-paged by `(created_at, id)`. Another tenant's rules are never read (the
repository filters on the JWT tenant and RLS backs it). `scope` is `platform` for a NULL tenant."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.core.rbac import Principal
from app.repositories.uow import tenant_uow


@dataclass(frozen=True, slots=True)
class RuleView:
    id: uuid.UUID
    marketplace: str
    category: str | None
    field: str
    rule_type: str
    severity: str
    version: int
    enabled: bool
    scope: Literal["platform", "tenant"]
    created_at: datetime


async def list_rules(
    principal: Principal,
    *,
    limit: int,
    after: tuple[datetime, uuid.UUID] | None,
    marketplace: str | None = None,
    category: str | None = None,
) -> tuple[list[RuleView], bool]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        rows, has_more = await uow.rules.list_page(
            limit=limit, after=after, marketplace=marketplace, category=category
        )
        views = [
            RuleView(
                id=r.id,
                marketplace=r.marketplace,
                category=r.category,
                field=r.field,
                rule_type=r.rule_type,
                severity=r.severity,
                version=r.version,
                enabled=r.enabled,
                scope="platform" if r.tenant_id is None else "tenant",
                created_at=r.created_at,
            )
            for r in rows
        ]
    return views, has_more
