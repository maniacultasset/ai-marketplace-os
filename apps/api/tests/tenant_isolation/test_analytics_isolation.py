"""S23 T8 (AC2; CANON.md:137): analytics as `app_rw` on `mos_iso`. Each metric read returns only
the caller's tenant's rows; a read missing a table's tenant criterion is refused before it runs;
and with the repository guard bypassed, RLS still returns the caller's rows only."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from tests.integration.analytics_support import (
    EXPECTED,
    FROM,
    TO,
    AnalyticsWorld,
    build_analytics_world,
)

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]

PAIRS = {
    "volume": ("listings_generated",),
    "decisions": ("approvals", "rejections"),
    "validation": ("validation_first_pass_passed", "validation_first_pass_total"),
    "publish": ("publish_succeeded", "publish_failed"),
}


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> AnalyticsWorld:
    return build_analytics_world(admin_conn)


def _bounds() -> tuple[datetime, datetime]:
    from app.services.analytics import DateRange

    return DateRange(FROM, TO).bounds()


async def _call(tenant: UUID, method: str) -> Any:
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work

    lo, hi = _bounds()
    async with unit_of_work(tenant) as session:
        return await getattr(AnalyticsRepository(session, tenant), method)(lo, hi)


def _expected(slug: str, keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for marketplace, per in EXPECTED[slug].items():
        values = tuple(per[k] for k in keys)
        if any(values):
            out[marketplace] = values[0] if len(values) == 1 else values
    return out


@pytest.mark.parametrize("method", sorted(PAIRS))
async def test_analytics_repo_sees_only_own_tenant(world: AnalyticsWorld, method: str) -> None:
    for slug in ("acme", "bharat"):
        assert await _call(world.tenants[slug], method) == _expected(slug, PAIRS[method])


async def test_analytics_raw_select_without_tenant_raises(world: AnalyticsWorld) -> None:
    """A variant of `volume` that drops the `listings` criterion is refused by `_scoped_read`,
    and the same statement unstamped is refused by the session guard."""
    from app.db.guard import CrossTenantQueryError
    from app.db.models.listings import Listing, ListingVersion
    from app.repositories.analytics import AnalyticsRepository
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import func, select

    acme = world.tenants["acme"]
    lo, hi = _bounds()
    async with unit_of_work(acme) as session:
        repo = AnalyticsRepository(session, acme)
        stmt = (
            select(Listing.marketplace, func.count())
            .select_from(ListingVersion)
            .join(Listing, Listing.id == ListingVersion.listing_id)
            .where(
                repo._criterion_for(ListingVersion),
                ListingVersion.created_at >= lo,
                ListingVersion.created_at < hi,
            )
            .group_by(Listing.marketplace)
        )
        with pytest.raises(CrossTenantQueryError, match="listings"):
            repo._scoped_read(stmt)
        with pytest.raises(CrossTenantQueryError):
            await session.execute(stmt)


async def test_analytics_rls_backstop(world: AnalyticsWorld) -> None:
    """The guard bypassed by hand (a stamped statement with no tenant clause at all): RLS still
    shows acme only acme's rows."""
    from app.db.guard import SCOPE_OK
    from app.db.models.listings import ListingVersion
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import func, select

    acme, bharat = world.tenants["acme"], world.tenants["bharat"]
    stmt = (
        select(ListingVersion.tenant_id, func.count())
        .group_by(ListingVersion.tenant_id)
        .execution_options(**{SCOPE_OK: True})
    )
    async with unit_of_work(acme) as session:
        seen = {tenant for tenant, _n in await session.execute(stmt)}
    assert seen == {acme}
    assert bharat not in seen
