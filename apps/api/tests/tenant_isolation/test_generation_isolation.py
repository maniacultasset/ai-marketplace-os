"""S18 T20 (AC 9, 16; CANON.md:137): generation as `app_rw` on `mos_iso`. A full acme generation
leaves every bharat row as it was, and the S18 repositories return nothing for bharat ids."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.generation_support import GenWorld, build_gen_world, make_generating

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
TOUCHED = (
    "listings",
    "listing_versions",
    "validation_runs",
    "review_decisions",
    "agent_runs",
    "job_runs",
    "idempotency_keys",
)


@pytest.fixture
async def world(clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch) -> GenWorld:
    return await build_gen_world(admin_conn, monkeypatch)


def _rows_hash(conn: Conn, tenant: uuid.UUID) -> dict[str, str]:
    out: dict[str, str] = {}
    for table in TOUCHED:
        rows = conn.execute(
            f"SELECT t::text FROM {table} t WHERE tenant_id = %s ORDER BY t::text",  # noqa: S608
            [tenant],
        ).fetchall()
        out[table] = hashlib.sha256("\n".join(str(r[0]) for r in rows).encode()).hexdigest()
    return out


async def test_iso_generation_touches_no_other_tenant_row(world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    bharat = world.tenant("bharat")
    other = await make_generating(world, email="operator@bharat.test")  # bharat has rows too
    before = _rows_hash(world.conn, bharat)
    register_bodies()
    accepted = await make_generating(world)
    await run_job(str(accepted.job_id))
    principal = world.principal("reviewer@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        again = await request_regeneration(
            uow, principal, str(uuid.uuid4()), accepted.listing_id, "shorter"
        )
    await run_job(str(again.job_id))
    assert world.listing(accepted.listing_id)["status"] == "validated"
    assert _rows_hash(world.conn, bharat) == before
    assert world.count("job_runs", "kind = 'listing_publish'") == 0  # inv 1, after the flows
    assert world.listing(other.listing_id)["status"] == "generating"


async def test_iso_generation_repositories_other_tenant_empty(world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow

    conn: Any = world.conn
    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    product = world.product("bharat", "KURTA-002")
    listing = f.listing(conn, bharat, product, category="ethnic_wear_kurta_sets")
    version = f.version(conn, bharat, listing)
    f.validation_run(conn, bharat, listing, listing_version_id=version)
    job = world.conn.execute(
        "INSERT INTO job_runs (tenant_id, kind, entity_type, entity_id, listing_id, payload_json, "
        "correlation_id) VALUES (%s, 'listing_generate', 'listing', %s, %s, '{}', %s) RETURNING id",
        [bharat, listing, listing, uuid.uuid4()],
    ).fetchone()
    assert job is not None
    f.review_decision(
        conn,
        bharat,
        listing,
        version,
        decision="regenerate_requested",
        regeneration_job_id=job[0],
        instructions="secret bharat instructions",
    )
    f.review_decision(
        conn,
        bharat,
        listing,
        version,
        decision="reject",
        reason_code="policy_violation",
        feedback="bharat note",
    )
    f.agent_run(conn, bharat, cost_usd=Decimal("9.000000"), started_at=datetime.now(UTC))
    async with tenant_uow(acme) as uow:
        assert await uow.listings.get(listing) is None
        assert await uow.listings.get_for_update(listing) is None
        assert await uow.listings.get_by_pair_for_update(product, "amazon") is None
        assert await uow.listings.latest_generate_job_id(listing) is None
        assert await uow.listing_versions.get(listing, version) is None
        assert await uow.validation_runs.for_version(version) == []
        assert await uow.review_decisions.instructions_for_job(job[0]) is None
        assert await uow.review_decisions.has_regenerate_for_job(job[0]) is False
        assert await uow.review_decisions.latest_reject_feedback(listing) == []
        assert await uow.product_facts.resolve(product) is None
        state = await uow.budget.tenant_state(datetime.now(UTC).date(), default_cap=Decimal("2.5"))
        assert state.used == Decimal("0")
        assert "bharat" not in str(await uow.brand_profile.for_prompt()).lower()


def test_iso_generation_repositories_subclass_tenant_base() -> None:
    from app.repositories import (
        brand_profile,
        budget,
        listings,
        product_facts,
        review_decisions,
        validation_runs,
    )
    from app.repositories.base import TenantScopedRepository

    for repo in (
        listings.ListingRepository,
        listings.ListingVersionRepository,
        validation_runs.ValidationRunRepository,
        review_decisions.ReviewDecisionRepository,
        product_facts.ProductFactsRepository,
        brand_profile.BrandPromptRepository,
        budget.BudgetRepository,
    ):
        assert issubclass(repo, TenantScopedRepository), repo
