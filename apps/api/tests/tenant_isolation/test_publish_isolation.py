"""S21 T24 (AC14; CANON.md:137): publish, submissions and the status-sync scheduler as `app_rw`
on `mos_iso`. acme never reaches a bharat listing, its submissions or its sync schedule; the
scheduler reads through `platform_scope` and writes in each listing's own tenant."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from tests.integration.generation_support import GenWorld, build_gen_world
from tests.integration.publish_support import PubWorld, approved, insert_account
from tests.integration.review_support import run_job

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
async def pw(
    clean_tenants: None, admin_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[PubWorld]:
    from app.adapters.fake import FakeAdapter
    from app.workers import publish as publish_worker
    from app.workers import readers

    world: GenWorld = await build_gen_world(admin_conn, monkeypatch)
    pub = PubWorld(world, FakeAdapter("amazon"))
    for slug in ("acme", "bharat"):
        pub.accounts[slug] = insert_account(world, slug)

    async def factory(*args: Any) -> Any:
        return pub.fake

    publish_worker.set_adapter_factory(factory)
    readers.set_reader_factory(factory)
    try:
        yield pub
    finally:
        publish_worker.set_adapter_factory(None)
        readers.set_reader_factory(None)


def _hash(conn: Any, table: str, tenant: uuid.UUID) -> str:
    rows = conn.execute(
        f"SELECT t::text FROM {table} t WHERE tenant_id = %s ORDER BY t::text",  # noqa: S608
        [tenant],
    ).fetchall()
    return hashlib.sha256("\n".join(str(r[0]) for r in rows).encode()).hexdigest()


async def _publish(pw: PubWorld, email: str, listing_id: uuid.UUID) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.publish import PublishItemIn, publish_batch

    principal = pw.world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        _batch, outcomes = await publish_batch(
            uow, principal, [PublishItemIn(listing_id)], http_key=str(uuid.uuid4())
        )
    return outcomes[0]


async def test_iso_publish_bharat_listing_404(pw: PubWorld) -> None:
    made = await approved(pw, maker="operator@bharat.test")
    outcome = await _publish(pw, "operator@acme.test", made.listing_id)
    assert outcome.http_status == 404
    assert pw.listing(made.listing_id)["status"] == "approved"


async def test_iso_bharat_submissions_404(pw: PubWorld) -> None:
    from app.core.errors import NotFoundError
    from app.services import submission_reads

    made = await approved(pw, maker="operator@bharat.test")
    outcome = await _publish(pw, "operator@bharat.test", made.listing_id)
    await run_job(outcome.job_id)
    with pytest.raises(NotFoundError):
        await submission_reads.page(
            pw.world.principal("operator@acme.test"), made.listing_id, after=None, limit=50
        )


async def test_iso_scheduler_writes_per_tenant(pw: PubWorld) -> None:
    from app.workers.periodic import schedule_status_sync

    made = await approved(pw, maker="operator@bharat.test")
    outcome = await _publish(pw, "operator@bharat.test", made.listing_id)
    await run_job(outcome.job_id)
    acme = pw.world.tenant("acme")
    before = {t: _hash(pw.conn, t, acme) for t in ("listings", "job_runs", "listing_submissions")}
    pw.conn.execute(
        "UPDATE listings SET next_sync_at = now() - interval '1 second' WHERE id = %s",
        [made.listing_id],
    )
    assert await schedule_status_sync() == 1
    job = pw.conn.execute(
        "SELECT tenant_id FROM job_runs WHERE listing_id = %s AND kind = 'listing_status_sync'",
        [made.listing_id],
    ).fetchone()
    assert job is not None and job[0] == pw.world.tenant("bharat")
    assert {t: _hash(pw.conn, t, acme) for t in before} == before
