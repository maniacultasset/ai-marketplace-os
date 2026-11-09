"""The quality dashboard (SI14, BE-13, AC10): `GET /analytics/quality` for the caller's tenant and
`GET /admin/quality` for the platform admin (every tenant, or one), over the inclusive UTC-day
range `from`..`to` (`analytics.resolve_range`: 30 days by default, at most 366).

Each answers first-pass, blocked and edit rates and the model cost per generated listing, in total
and per marketplace, with the 20 most edited fields; the admin view adds a per-tenant breakdown of
ids and counts only. A rate is null while its denominator is 0. The tenant read keeps the S23 rules
(READ ONLY, one 5 s budget, at most 2 in flight per tenant); the platform read runs on the read-only
platform session `app.workers.rollup.rollup_session()` (R5) with the same 5 s cap.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Final

from app.core.rbac import Principal
from app.repositories.quality import QualityCounts, QualityRead, QualityRepository, platform_quality
from app.repositories.transactions import unit_of_work
from app.services.analytics import (
    MARKETPLACE_ORDER,
    DateRange,
    inflight,
    rate,
    resolve_range,
    utc_now,
)

__all__ = ["get_platform_quality", "get_quality", "summarize_quality"]

MAX_TENANTS: Final = 200
COST_QUANTUM: Final = Decimal("0.000001")


def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _cost_per(counts: QualityCounts) -> float | None:
    if counts.generated == 0:
        return None
    return float((counts.cost / counts.generated).quantize(COST_QUANTUM))


def _rates(counts: QualityCounts) -> dict[str, Any]:
    return {
        "listings_generated": counts.generated,
        "first_pass_rate": _float(rate(counts.first_pass, counts.approved)),
        "blocked_rate": _float(rate(min(counts.blocked, counts.generated), counts.generated)),
        "edit_rate": _float(rate(counts.edited, counts.approved)),
    }


def summarize_quality(
    period: DateRange, read: QualityRead, *, platform: bool = False
) -> dict[str, Any]:
    per = read.by_marketplace()
    totals = QualityCounts()
    for counts in per.values():
        totals.add(counts)
    body: dict[str, Any] = {
        "from": period.start,
        "to": period.end,
        **_rates(totals),
        "cost_per_listing_usd": _cost_per(totals),
        "by_marketplace": [
            {
                "marketplace": market,
                **_rates(per[market]),
                "cost_usd": float(per[market].cost),
                "cost_per_listing_usd": _cost_per(per[market]),
            }
            for market in MARKETPLACE_ORDER
            if market in per
        ],
        "edited_fields": [{"path": path, "count": n} for path, n in read.edited_fields],
        "by_tenant": None,
    }
    if platform:
        tenants = sorted(
            read.by_tenant().items(), key=lambda item: (-item[1].generated, str(item[0]))
        )
        body["by_tenant"] = [
            {"tenant_id": str(tenant), **_rates(counts), "cost_usd": float(counts.cost)}
            for tenant, counts in tenants[:MAX_TENANTS]
        ]
    return body


async def get_quality(principal: Principal, from_: date | None, to: date | None) -> dict[str, Any]:
    period = resolve_range(from_, to, today=utc_now().date())
    tenant = principal.tenant_id
    async with inflight(tenant), unit_of_work(tenant, user_id=principal.user_id) as session:
        repo = QualityRepository(session, tenant)
        await repo.bounded()
        read = await repo.quality(*period.bounds())
    return summarize_quality(period, read)


async def get_platform_quality(
    principal: Principal, from_: date | None, to: date | None, tenant_id: uuid.UUID | None
) -> dict[str, Any]:
    from app.workers.rollup import rollup_session

    period = resolve_range(from_, to, today=utc_now().date())
    async with inflight(principal.tenant_id), rollup_session() as session:
        read = await platform_quality(session, *period.bounds(), tenant_id)
    return summarize_quality(period, read, platform=True)
