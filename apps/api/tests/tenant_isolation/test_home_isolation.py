"""FB6 (CANON.md:137): the Home summary reads as `app_rw` on `mos_iso`. Each read returns only the
caller's tenant's rows, and with the repository guard bypassed RLS still does."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from tests.integration.generation_support import GenWorld, build_gen_world
from tests.integration.home_support import BUCKET_DELTA, scenario

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
async def world(clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch) -> GenWorld:
    return await build_gen_world(admin_conn, monkeypatch)


async def _reads(tenant: uuid.UUID) -> dict[str, Any]:
    from app.repositories.home import home_reads
    from app.repositories.transactions import unit_of_work
    from app.services.home import tally

    async with unit_of_work(tenant) as session:
        reads = home_reads(session, tenant)
        whole, _ = tally(await reads.listings.bucket_groups())
        return {
            "total": whole.total,
            "blocked": [r.listing_id for r in await reads.listings.blocked_rows(50)],
            "approve": [
                r.listing_id
                for r in await reads.listings.ready_rows(
                    approve=True, marketplaces=["amazon", "flipkart"], limit=50
                )
            ],
            "tasks": await reads.tasks.open_counts(),
            "questions": [t.task_id for t in await reads.tasks.blocking_rows(50)],
            "fixes": [x.listing_id for x in await reads.issues.recent_fixes(50)],
            "jobs": len(await reads.jobs.recent(50)),
            "products": await reads.products.count(),
            "accounts": await reads.accounts.current(),
        }


async def test_iso_home_reads_see_only_own_tenant(world: GenWorld) -> None:
    acme = world.tenant("acme")
    before = await _reads(acme)
    s = scenario(world, "bharat")
    assert await _reads(acme) == before
    theirs = await _reads(world.tenant("bharat"))
    assert s.blocked in theirs["blocked"] and s.blocked not in before["blocked"]
    assert s.question in theirs["questions"]
    assert theirs["total"] - BUCKET_DELTA["total"] >= 0


async def test_iso_rls_backstop_on_the_home_tables(world: GenWorld) -> None:
    """The guard bypassed by hand (a stamped statement with no tenant clause): RLS still shows
    acme only acme's rows of every table the Home summary reads."""
    from app.db.guard import SCOPE_OK
    from app.db.models.issues import HumanTask, ListingIssue
    from app.db.models.listings import Listing
    from app.db.models.ops import JobRun
    from app.repositories.transactions import unit_of_work
    from sqlalchemy import select

    scenario(world, "bharat")
    acme = world.tenant("acme")
    async with unit_of_work(acme) as session:
        for model in (Listing, ListingIssue, HumanTask, JobRun):
            stmt = select(model.tenant_id).distinct().execution_options(**{SCOPE_OK: True})
            assert set((await session.execute(stmt)).scalars()) <= {acme}, model.__name__


async def test_iso_brand_overview_counts_each_brand_inside_itself(world: GenWorld) -> None:
    """Item 18: the cross-brand overview as `app_rw` lists only the caller's memberships, and
    each brand's counts equal that brand's own reads under its own tenant context."""
    from app.services.home import get_brands
    from tests.integration import factories as f

    scenario(world, "bharat")
    owner = world.principal("owner@acme.test")
    alone = await get_brands(owner)
    assert [b["tenant_id"] for b in alone["brands"]] == [world.tenant("acme")]

    f.insert(
        world.conn,
        "tenant_memberships",
        tenant_id=world.tenant("bharat"),
        user_id=owner.user_id,
        role="reviewer",
    )
    world.conn.commit()
    both = {b["tenant_id"]: b for b in (await get_brands(owner))["brands"]}
    assert set(both) == {world.tenant("acme"), world.tenant("bharat")}
    for slug in ("acme", "bharat"):
        reads = await _reads(world.tenant(slug))
        assert both[world.tenant(slug)]["listings"]["total"] == reads["total"], slug
        assert both[world.tenant(slug)]["products"] == reads["products"], slug
    assert both[world.tenant("bharat")]["role"] == "reviewer"
    assert both[world.tenant("acme")]["current"] is True
