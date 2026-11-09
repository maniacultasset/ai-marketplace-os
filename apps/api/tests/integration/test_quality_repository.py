"""SI14 T19 (AC10): the quality reads: the formulas, the range, tenant isolation, the platform
read and the 5 s budget."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .quality_support import RANGE, blocked_by_worker, scenario

__all__ = ["gen_world"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


async def _tenant_read(world: GenWorld, slug: str) -> Any:
    from app.repositories.quality import QualityRepository
    from app.repositories.transactions import unit_of_work

    tenant = world.tenant(slug)
    async with unit_of_work(tenant) as session:
        repo = QualityRepository(session, tenant)
        await repo.bounded()
        return await repo.quality(*RANGE)


async def _platform_read(tenant_id: uuid.UUID | None) -> Any:
    from app.db.session import platform_scope
    from app.repositories.quality import platform_quality

    async with platform_scope() as session:
        await session.execute(__import__("sqlalchemy").text("SET TRANSACTION READ ONLY"))
        return await platform_quality(session, *RANGE, tenant_id)


async def test_formulas_on_a_clean_tenant(gen_world: GenWorld) -> None:
    scenario(gen_world, "bharat")
    read = await _tenant_read(gen_world, "bharat")
    per = read.by_marketplace()
    amazon, flipkart = per["amazon"], per["flipkart"]
    assert (amazon.generated, amazon.approved, amazon.first_pass, amazon.edited) == (1, 1, 1, 0)
    assert amazon.blocked == 1  # the audited user move, counted though it moved on
    assert amazon.cost == Decimal("0.02")
    assert (flipkart.generated, flipkart.approved, flipkart.first_pass, flipkart.edited) == (
        1,
        1,
        0,
        1,
    )
    assert flipkart.blocked == 0
    assert read.edited_fields == [("bullets.0", 1), ("title", 1)]


async def test_the_worker_path_counts_blocked(gen_world: GenWorld) -> None:
    before = (await _tenant_read(gen_world, "acme")).by_marketplace().get("amazon")
    blocked_before = 0 if before is None else before.blocked
    generated_before = 0 if before is None else before.generated
    await blocked_by_worker(gen_world)
    after = (await _tenant_read(gen_world, "acme")).by_marketplace()["amazon"]
    assert after.blocked == blocked_before + 1
    assert after.generated == generated_before + 1


async def test_another_tenants_rows_never_count(gen_world: GenWorld) -> None:
    before = await _tenant_read(gen_world, "acme")
    scenario(gen_world, "bharat")
    after = await _tenant_read(gen_world, "acme")
    assert after.rows == before.rows and after.edited_fields == before.edited_fields


async def test_an_empty_range_reads_nothing(gen_world: GenWorld) -> None:
    from datetime import datetime

    from app.repositories.quality import QualityRepository
    from app.repositories.transactions import unit_of_work

    scenario(gen_world, "bharat")
    tenant = gen_world.tenant("bharat")
    async with unit_of_work(tenant) as session:
        read = await QualityRepository(session, tenant).quality(
            datetime(2020, 1, 1, tzinfo=RANGE[0].tzinfo),
            datetime(2020, 1, 2, tzinfo=RANGE[0].tzinfo),
        )
    assert read.rows == {} and read.edited_fields == []


async def test_platform_read_sums_and_filters(gen_world: GenWorld) -> None:
    scenario(gen_world, "bharat")
    acme = await _tenant_read(gen_world, "acme")
    bharat = await _tenant_read(gen_world, "bharat")
    every = await _platform_read(None)
    assert every.by_tenant()[gen_world.tenant("bharat")].generated == 2
    assert set(every.by_tenant()) >= {gen_world.tenant("bharat")}
    expected = sum(c.generated for c in (*acme.rows.values(), *bharat.rows.values()))
    assert sum(c.generated for c in every.rows.values()) >= expected
    only = await _platform_read(gen_world.tenant("bharat"))
    assert only.rows == bharat.rows


async def test_the_read_budget_maps_to_the_budget_error(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.repositories.quality as quality
    from app.repositories.analytics import AnalyticsBudgetError

    monkeypatch.setattr(quality, "TENANT_COUNTS", "SELECT pg_sleep(6)")
    with pytest.raises(AnalyticsBudgetError):
        await _tenant_read(gen_world, "bharat")
