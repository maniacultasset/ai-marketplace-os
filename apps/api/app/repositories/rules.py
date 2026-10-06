"""`RuleRepository`: a tenant's own rules plus the platform rules (`tenant_id IS NULL`)."""

from __future__ import annotations

from sqlalchemy import or_

from app.db.models.rules import Rule
from app.repositories.base import TenantScopedRepository


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
