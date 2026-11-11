"""SI12 B7: what each marketplace needs for a category, for the "Needed" chips.

`requirements(uow, category)` answers, per marketplace, the sorted and de-duplicated union of
the adapter's `REQUIRED_FACTS`, the current category template's `required_attributes` (none when
the category has no template) and the adapter's reviewed `EXTRA_NEEDS` (CP-D1: marketplace
knowledge stays in the adapters; this module only unions what they export). With
`PHOTO_VIEWS_ENABLED` on (feedback 7 and 8), `size_measurements` joins a marketplace's list where
the photo view checklist requires the size chart, and the checklist itself is returned. Every entry is an
attribute dictionary key, so the client can match a chip to its field. Platform rows only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from mkos_schemas.enums import Marketplace

from app.adapters.amazon import constants as amazon
from app.adapters.flipkart import constants as flipkart
from app.core.config import get_settings
from app.core.rbac import Principal
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services import photo_views
from app.services.category_gate import UNCATEGORIZED, category_enabled

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
    # feedback 7: the category's photo view checklist (empty while PHOTO_VIEWS_ENABLED is off)
    photo_views: tuple[photo_views.MergedView, ...] = ()


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
    """SI14: a category switched off (lamps while CATEGORY_LAMPS_ENABLED is off) answers as an
    unknown one: the marketplace facts only."""
    enabled = category_enabled(category, get_settings())
    views = photo_views.enabled()
    slug = category if enabled else UNCATEGORIZED
    out: dict[Marketplace, tuple[str, ...]] = {}
    for marketplace in NEEDS:
        current = await uow.templates.current(marketplace.value, category) if enabled else None
        required = () if current is None else tuple(current.template.required_attributes)
        if views and photo_views.size_chart_required(marketplace, slug):
            # feedback 8: the checklist (data, not an adapter) requires the seller's size chart
            required = (*required, photo_views.SIZE_CHART_KEY)
        out[marketplace] = needs_for(marketplace, slug, required)
    return Requirements(
        category=category,
        amazon=out[Marketplace.AMAZON],
        flipkart=out[Marketplace.FLIPKART],
        photo_views=photo_views.merged(slug) if views else (),
    )


async def get_requirements(principal: Principal, category: str) -> Requirements:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await requirements(uow, category)
