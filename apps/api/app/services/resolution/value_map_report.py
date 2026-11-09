"""The value-map report (SI13 T15, T17): every tenant's refused-then-live values, each read in its
own unit of work (RLS), aggregated in memory by the pure `value_maps.propose`. Read-only; used by
`errors value-maps` and the ops errors page, which pass their own platform session. At most 100
tenants and 500 observations each."""

from __future__ import annotations

from typing import Any, Final

from app.adapters.amazon.aliases import COLOUR_FAMILY_ALIASES, SIZE_ALIASES
from app.repositories.error_catalog import value_map_inputs
from app.repositories.uow import tenant_uow
from app.repositories.value_observations import ValueObservationRepository
from app.services.resolution.value_maps import AXIS_KEYS, Observation, propose

VALUE_MAP_TENANTS: Final = 100


async def value_map_report(platform: Any, min_listings: int) -> tuple[list[Any], list[Any]]:
    tenants, enums, enum_keys = await value_map_inputs(platform, tenants=VALUE_MAP_TENANTS)
    keys = sorted(AXIS_KEYS | enum_keys)
    observations: list[Observation] = []
    for tenant_id in tenants:
        async with tenant_uow(tenant_id) as uow:
            found = await ValueObservationRepository(uow.session, tenant_id).rejected_then_live(
                keys
            )
        observations += [
            Observation(
                o.marketplace,
                o.product_type,
                o.key,
                o.v,
                o.w,
                o.child,
                str(o.listing_id),
                str(tenant_id),
            )
            for o in found
        ]
    return propose(
        observations,
        {k: sorted(v) for k, v in enums.items()},
        {**SIZE_ALIASES, **COLOUR_FAMILY_ALIASES},
        allowlist=enum_keys,
        min_listings=min_listings,
    )
