"""S19 T16 (AC 17; CANON.md:137): review as `app_rw` on `mos_iso`. An acme approve and reject
batch leave every bharat row as it was, and the S19 repositories (batches, decisions, the audit
history, the listing page and `lock_many`) return nothing of bharat's to acme."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import psycopg
import pytest
from tests.integration.generation_support import GenWorld, build_gen_world, make_generating

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
TOUCHED = (
    "listings",
    "listing_versions",
    "validation_runs",
    "review_decisions",
    "batches",
    "audit_log",
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


async def _generated(world: GenWorld, email: str) -> tuple[uuid.UUID, uuid.UUID]:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    register_bodies()
    accepted = await make_generating(world, email=email)
    await run_job(str(accepted.job_id))
    row = world.listing(accepted.listing_id)
    return accepted.listing_id, row["current_version_id"]


async def _batch(world: GenWorld, email: str, items: list[Any]) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.review import approve_batch

    principal = world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        return await approve_batch(uow, principal, items, http_key=str(uuid.uuid4()))


async def test_iso_review_touches_no_other_tenant_row(world: GenWorld) -> None:
    from app.services.review import ReviewItem

    bharat = world.tenant("bharat")
    b_listing, b_version = await _generated(world, "operator@bharat.test")
    before = _rows_hash(world.conn, bharat)
    a_listing, a_version = await _generated(world, "operator@acme.test")
    outcome = await _batch(
        world,
        "reviewer@acme.test",
        [
            ReviewItem(a_listing, a_version, "approve"),
            ReviewItem(b_listing, b_version, "approve"),  # bharat's: item 404 for acme
        ],
    )
    assert [i.http_status for i in outcome.items] == [200, 404]
    assert outcome.items[1].listing_status is None
    assert _rows_hash(world.conn, bharat) == before
    assert world.listing(b_listing)["status"] == "validated"


async def test_iso_review_repositories_other_tenant_empty(world: GenWorld) -> None:
    from app.repositories.listings import ListingFilter
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem

    b_listing, b_version = await _generated(world, "operator@bharat.test")
    outcome = await _batch(
        world,
        "reviewer@bharat.test",
        [ReviewItem(b_listing, b_version, "reject", reason_code="other", feedback="No.")],
    )
    assert outcome.items[0].http_status == 200
    principal = world.principal("reviewer@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        assert await uow.listings.lock_many([b_listing]) == {}
        rows, more = await uow.listings.page(ListingFilter(), after=None, limit=200)
        assert b_listing not in {r.id for r in rows} and more is False
        assert await uow.batches.get_by_id(outcome.batch_id) is None
        history, _ = await uow.audit_log.page_for_listing(b_listing, after=None, limit=50)
        assert history == []
        assert await uow.review_decisions.latest_reject_feedback(b_listing, 5) == []
        assert await uow.listing_versions.get_many([b_version]) == {}
        assert await uow.listing_versions.chain_links(b_listing) == []
        assert await uow.members.role_for_share(world.user("reviewer@bharat.test")) is None
