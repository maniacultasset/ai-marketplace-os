"""SI12 B7: what each marketplace needs for a category, for the "Needed" chips.

`requirements(uow, category)` answers, per marketplace, the sorted and de-duplicated union of
the adapter's `REQUIRED_FACTS`, the current category template's `required_attributes` (none when
the category has no template) and the adapter's reviewed `EXTRA_NEEDS` (CP-D1: marketplace
knowledge stays in the adapters; this module only unions what they export). Every entry is an
attribute dictionary key, so the client can match a chip to its field. Platform rows only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from mkos_schemas.enums import Marketplace

from app.adapters.amazon import constants as amazon
from app.adapters.flipkart import constants as flipkart
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork, tenant_uow

__all__ = ["NEEDS", "Requirements", "get_requirements", "needs_for", "requirements"]


@dataclass(frozen=True, slots=True)
class _Needs:
    required_facts: tuple[str, ...]
    extra: Mapping[str | None, tuple[str, ...]]


NEEDS: Final[Mapping[Marketplace, _Needs]] = {
    Marketplace.AMAZON: _Needs(amazon.REQUIRED_FACTS, amazon.EXTRA_NEEDS),
    Marketplace.FLIPKART: _Needs(flipkart.REQUIRED_FACTS, flipkart.EXTRA_NEEDS),
}


@dataclass(frozen=True, slots=True)
class Requirements:
    category: str
    amazon: tuple[str, ...]
    flipkart: tuple[str, ...]


def needs_for(
    marketplace: Marketplace, category: str, template_required: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The sorted union for one marketplace (pure)."""
    needs = NEEDS[marketplace]
    keys = {
        *needs.required_facts,
        *template_required,
        *needs.extra.get(None, ()),
        *needs.extra.get(category, ()),
    }
    return tuple(sorted(keys))


async def requirements(uow: TenantUnitOfWork, category: str) -> Requirements:
    out: dict[Marketplace, tuple[str, ...]] = {}
    for marketplace in NEEDS:
        current = await uow.templates.current(marketplace.value, category)
        required = () if current is None else tuple(current.template.required_attributes)
        out[marketplace] = needs_for(marketplace, category, required)
    return Requirements(
        category=category, amazon=out[Marketplace.AMAZON], flipkart=out[Marketplace.FLIPKART]
    )


async def get_requirements(principal: Principal, category: str) -> Requirements:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await requirements(uow, category)
