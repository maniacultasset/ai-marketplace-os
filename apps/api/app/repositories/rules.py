"""`RuleRepository`: a tenant's own rules plus the platform rules (`tenant_id IS NULL`).

- `list_effective`: the enabled rows for a scope (S9).
- `list_for_resolution`: every row the resolver needs (S14b T5): platform plus own, disabled rows
  included (they count in `rules_version`, DATABASE.md:507), as wire `Rule`s with their row
  `revision` and stored `subject`. A stored `rule_def` that no longer validates raises the
  pydantic error: the caller fails closed.
- `list_page`: the `GET /rules` keyset page, `(created_at, id)` ascending.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mkos_schemas.rules import Rule as WireRule
from sqlalchemy import literal, or_, tuple_

from app.db.models.rules import Rule
from app.repositories.base import TenantScopedRepository

PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 200


@dataclass(frozen=True, slots=True)
class RuleRecord:
    """One fetched `rules` row: the wire rule, its `revision`, owner and stored `subject`."""

    rule: WireRule
    revision: int
    tenant_id: UUID | None
    subject: str


def to_wire(row: Rule) -> WireRule:
    return WireRule.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "marketplace": row.marketplace,
            "category": row.category,
            "field": row.field,
            "severity": row.severity,
            "version": row.version,
            "enabled": row.enabled,
            "rule_def": row.rule_def,
        }
    )


class RuleRepository(TenantScopedRepository[Rule]):
    model = Rule

    async def list_effective(self, marketplace: str, category: str | None) -> list[Rule]:
        """Enabled rules for `marketplace` that apply to `category` (category-wide rules,
        `category IS NULL`, always apply): the tenant's own and the platform's."""
        stmt = self._select(include_platform=True).where(
            Rule.enabled.is_(True), Rule.marketplace == marketplace
        )
        if category is None:
            stmt = stmt.where(Rule.category.is_(None))
        else:
            stmt = stmt.where(or_(Rule.category == category, Rule.category.is_(None)))
        stmt = stmt.order_by(Rule.field, Rule.rule_type, Rule.id)
        return list((await self.db.execute(stmt)).scalars())

    async def list_for_resolution(self, marketplace: str, category: str) -> list[RuleRecord]:
        """Every platform and own row for `marketplace` whose category is `category` or NULL,
        disabled rows included, in id order (one statement)."""
        stmt = (
            self._select(include_platform=True)
            .where(
                Rule.marketplace == marketplace,
                or_(Rule.category == category, Rule.category.is_(None)),
            )
            .order_by(Rule.id)
        )
        rows = (await self.db.execute(stmt)).scalars()
        return [
            RuleRecord(
                rule=to_wire(row),
                revision=row.revision,
                tenant_id=row.tenant_id,
                subject=row.subject,
            )
            for row in rows
        ]

    async def list_page(
        self,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None,
        marketplace: str | None = None,
        category: str | None = None,
    ) -> tuple[builtins.list[Rule], bool]:
        """Platform and own rows, `(created_at, id)` ascending, `limit + 1` fetched."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an int")
        if not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be in {PAGE_LIMIT_MIN}..{PAGE_LIMIT_MAX}")
        stmt = self._select(include_platform=True)
        if marketplace is not None:
            stmt = stmt.where(Rule.marketplace == marketplace)
        if category is not None:
            stmt = stmt.where(Rule.category == category)
        if after is not None:
            created_at, row_id = after
            bound = tuple_(literal(created_at, Rule.created_at.type), literal(row_id, Rule.id.type))
            stmt = stmt.where(tuple_(Rule.created_at, Rule.id) > bound)
        stmt = stmt.order_by(Rule.created_at, Rule.id).limit(limit + 1)
        rows = builtins.list((await self.db.execute(stmt)).scalars())
        return rows[:limit], len(rows) > limit
