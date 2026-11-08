"""The value-map report (SI13 T15, T17): every tenant's refused-then-live values, each read in its
own unit of work (RLS), aggregated in memory by the pure `value_maps.propose`. Read-only; used by
`errors value-maps` and the ops errors page. At most 100 tenants and 500 observations each."""

from __future__ import annotations

from typing import Any, Final

VALUE_MAP_TENANTS: Final = 100


async def value_map_report(min_listings: int) -> tuple[list[Any], list[Any]]:
    """Every tenant's refused-then-live values (each in its own unit of work), aggregated in
    memory through the pure `value_maps.propose`."""
    from sqlalchemy import select

    from app.adapters.amazon.aliases import COLOUR_FAMILY_ALIASES, SIZE_ALIASES
    from app.db.models.platform import CategoryTemplate
    from app.db.models.tenancy import Tenant
    from app.db.session import platform_scope
    from app.repositories.uow import tenant_uow
    from app.repositories.value_observations import ValueObservationRepository
    from app.services.resolution.value_maps import AXIS_KEYS, Observation, propose

    enums: dict[str, set[str]] = {}
    enum_keys: set[str] = set()
    async with platform_scope() as session:
        tenants = list(
            (
                await session.execute(
                    select(Tenant.id).order_by(Tenant.id).limit(VALUE_MAP_TENANTS)
                )
            ).scalars()
        )
        rows = await session.execute(
            select(CategoryTemplate.attributes).where(CategoryTemplate.is_current.is_(True))
        )
        for (attributes,) in rows:
            for key, spec in ((attributes or {}).get("specs") or {}).items():
                if not isinstance(spec, dict) or spec.get("value_type") != "enum":
                    continue
                enum_keys.add(str(key))
                values = list(spec.get("allowed_values") or []) + list(
                    (spec.get("value_map") or {}).values()
                )
                enums.setdefault(str(key), set()).update(str(v) for v in values)
    keys = sorted(AXIS_KEYS | enum_keys)
    observations: list[Any] = []
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
    aliases = {**SIZE_ALIASES, **COLOUR_FAMILY_ALIASES}
    return propose(
        observations,
        {k: sorted(v) for k, v in enums.items()},
        aliases,
        allowlist=enum_keys,
        min_listings=min_listings,
    )
