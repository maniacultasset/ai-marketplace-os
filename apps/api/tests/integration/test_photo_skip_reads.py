"""SI3 T4 (AC3, AC10): the skip rule's read, the newest recorded photo set of a product, as app_rw."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .jobs_support import insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
OPERATOR = "operator@acme.test"
SET_A = [["11111111-1111-4111-8111-111111111111", "ha"]]
SET_B = [
    ["22222222-2222-4222-8222-222222222222", "hb"],
    ["33333333-3333-4333-8333-333333333333", "hc"],
]


async def _listing(world: GenWorld, sku: str, marketplace: str) -> uuid.UUID:
    accepted = await make_generating(world, sku=sku, marketplace=marketplace)
    return accepted.listing_id  # type: ignore[no-any-return]


def _job(
    world: GenWorld,
    tenant: uuid.UUID,
    listing_id: uuid.UUID,
    result: dict[str, Any] | None,
    *,
    finished: str = "2026-11-07T10:00:00Z",
    kind: str = "listing_generate",
    status: str = "succeeded",
) -> uuid.UUID:
    failure: dict[str, Any] = {}
    if status == "failed":
        failure = {
            "error_code": "GENERATION_FAILED",
            "error_report_json": Jsonb(
                {"status": "failed", "error_code": "GENERATION_FAILED", "errors": []}
            ),
        }
    return insert_job(
        world.conn,
        tenant,
        kind=kind,
        entity_type="listing",
        entity_id=listing_id,
        listing_id=listing_id,
        status=status,
        result_json=Jsonb(result) if result is not None else None,
        finished_at=finished,
        started_at=finished,
        attempt=1,
        **failure,
    )


async def _read(tenant: uuid.UUID, product_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant) as uow:
        return await uow.job_runs.latest_photo_set(product_id)


async def test_newest_set_across_both_marketplaces(gen_world: GenWorld) -> None:
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    amazon = await _listing(gen_world, "KURTA-002", "amazon")
    flipkart = await _listing(gen_world, "KURTA-002", "flipkart")
    assert await _read(acme, product) is None
    _job(gen_world, acme, amazon, {"photo_set": SET_A}, finished="2026-11-07T10:00:00Z")
    _job(gen_world, acme, flipkart, {"photo_set": SET_B}, finished="2026-11-07T11:00:00Z")
    assert await _read(acme, product) == tuple(tuple(p) for p in SET_B)


async def test_ignores_jobs_without_a_set_unfinished_other_kinds_and_products(
    gen_world: GenWorld,
) -> None:
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    other_product = gen_world.product("acme", "KURTA-001")
    mine = await _listing(gen_world, "KURTA-002", "amazon")
    other = await _listing(gen_world, "KURTA-001", "flipkart")
    _job(gen_world, acme, mine, {"photo_set": SET_A}, finished="2026-11-07T09:00:00Z")
    _job(gen_world, acme, mine, {"other": 1}, finished="2026-11-07T12:00:00Z")
    # the live (queued) job make_generating enqueued: unfinished, so never read
    gen_world.conn.execute(
        "UPDATE job_runs SET result_json = %s WHERE listing_id = %s AND status = 'queued'",
        [Jsonb({"photo_set": SET_B}), mine],
    )
    _job(gen_world, acme, mine, {"photo_set": SET_B}, kind="listing_status_sync")
    _job(gen_world, acme, other, {"photo_set": SET_B}, finished="2026-11-07T13:00:00Z")
    assert await _read(acme, product) == tuple(tuple(p) for p in SET_A)
    assert await _read(acme, other_product) == tuple(tuple(p) for p in SET_B)


async def test_a_failed_job_with_a_set_counts(gen_world: GenWorld) -> None:
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    listing = await _listing(gen_world, "KURTA-002", "amazon")
    _job(gen_world, acme, listing, {"photo_set": SET_A}, status="failed")
    assert await _read(acme, product) == tuple(tuple(p) for p in SET_A)


@pytest.mark.parametrize(
    "bad",
    [
        {"photo_set": "x"},
        {"photo_set": [["only-one"]]},
        {"photo_set": [[1, 2]]},
        {"photo_set": None},
    ],
)
async def test_malformed_set_reads_as_none(gen_world: GenWorld, bad: dict[str, Any]) -> None:
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    listing = await _listing(gen_world, "KURTA-002", "amazon")
    _job(gen_world, acme, listing, bad)
    assert await _read(acme, product) is None


async def test_empty_set_is_a_set(gen_world: GenWorld) -> None:
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    listing = await _listing(gen_world, "KURTA-002", "amazon")
    _job(gen_world, acme, listing, {"photo_set": []})
    assert await _read(acme, product) == ()


async def test_tenant_isolation(gen_world: GenWorld) -> None:
    acme, bharat = gen_world.tenant("acme"), gen_world.tenant("bharat")
    bharat_product = gen_world.scalar(
        "SELECT id FROM products WHERE tenant_id = %s ORDER BY sku LIMIT 1", [bharat]
    )
    bharat_listing = uuid.uuid4()
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "INSERT INTO listings (id, tenant_id, product_id, marketplace, category, status) "
            "SELECT %s, tenant_id, id, 'amazon', category, 'created' FROM products WHERE id = %s",
            [bharat_listing, bharat_product],
        )
    _job(gen_world, bharat, bharat_listing, {"photo_set": SET_A})
    assert await _read(bharat, bharat_product) == tuple(tuple(p) for p in SET_A)
    assert await _read(acme, bharat_product) is None


def _index_probes(plan: Any) -> list[tuple[str, str]]:
    """(index name, index condition) of every index probe in an EXPLAIN (FORMAT JSON) plan."""
    found: list[tuple[str, str]] = []
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if "Index Name" in node:
                found.append((node["Index Name"], str(node.get("Index Cond", ""))))
            stack.extend(node.values())
    return found


async def test_the_read_probes_job_runs_by_entity(gen_world: GenWorld) -> None:
    """Round 1 F3: the read reaches job_runs through ix_job_runs_entity (tenant, entity_type,
    entity_id) per listing of the product, never by a scan of the tenant's job history."""
    from app.db.session import get_engine
    from sqlalchemy import event

    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    amazon = await _listing(gen_world, "KURTA-002", "amazon")
    _job(gen_world, acme, amazon, {"photo_set": SET_A})
    sent: list[tuple[str, Any]] = []

    def record(_c: Any, _cur: Any, statement: str, params: Any, *_rest: Any) -> None:
        if "FROM job_runs JOIN listings" in statement:
            sent.append((statement, params))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await _read(acme, product) == tuple(tuple(p) for p in SET_A)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    ((sql, params),) = sent
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL enable_seqscan = off")
        cursor = psycopg.ClientCursor(gen_world.conn)
        row = cursor.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
    assert row is not None
    probes = _index_probes(row[0])
    assert any(
        name == "ix_job_runs_entity" and "entity_id" in cond for name, cond in probes
    ), json.dumps(row[0])


async def test_a_job_on_another_entity_of_the_listing_is_not_read(gen_world: GenWorld) -> None:
    """The read follows the generate job's entity (the listing), as `enqueue_generate` writes it."""
    acme = gen_world.tenant("acme")
    product = gen_world.product("acme", "KURTA-002")
    amazon = await _listing(gen_world, "KURTA-002", "amazon")
    _job(gen_world, acme, amazon, {"photo_set": SET_A}, finished="2026-11-07T10:00:00Z")
    insert_job(
        gen_world.conn,
        acme,
        kind="listing_generate",
        entity_type="image",
        entity_id=uuid.uuid4(),
        listing_id=amazon,
        status="succeeded",
        result_json=Jsonb({"photo_set": SET_B}),
        finished_at="2026-11-07T11:00:00Z",
        started_at="2026-11-07T11:00:00Z",
        attempt=1,
    )
    assert await _read(acme, product) == tuple(tuple(p) for p in SET_A)
