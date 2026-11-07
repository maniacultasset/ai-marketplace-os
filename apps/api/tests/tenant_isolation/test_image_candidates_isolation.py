"""SI7 T9 (AC12; CANON.md:137): the image candidate repository as `app_rw` on `mos_iso`: every
read with another tenant's ids is empty, every write changes nothing, and a write through another
tenant's unit of work is refused."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from tests.integration import factories as f
from tests.integration.auth_support import World, build_world

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
HASH = "e" * 64


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


def _candidate(conn: Conn, tenant: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    p = f.product(conn, tenant, category="kurtas")
    src = f.image(conn, tenant, p, slot="main", storage_key=f"{tenant}/{p}/images/x.jpg")
    job = uuid.uuid4()
    cid = f.image_candidate(conn, tenant, p, src, source_hash=HASH, job_id=job)
    return p, src, cid


async def test_iso_candidate_reads_and_writes(world: World, admin_conn: Conn) -> None:
    from app.repositories.uow import tenant_uow

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p, src, cid = _candidate(admin_conn, bharat)
    job = admin_conn.execute("SELECT job_id FROM image_candidates WHERE id = %s", [cid]).fetchone()
    assert job is not None
    job_id = job[0]
    assert isinstance(job_id, uuid.UUID)
    async with tenant_uow(acme) as uow:
        repo = uow.image_candidates
        assert await repo.get(cid) is None
        assert await repo.get_for_update(cid) is None
        assert await repo.find_cached(p, "fix", HASH, "fix/v1") is None
        assert await repo.list_for_product(p) == []
        assert await repo.count_ai_today(datetime.now(UTC).date()) == 0
        assert await repo.expired(datetime.now(UTC) + timedelta(days=1), 10) == []
        assert not await repo.set_job(cid, uuid.uuid4())
        assert not await repo.mark_ready(cid, job_id, storage_key="k", width=1, height=1)
        assert not await repo.mark_failed(cid, job_id, "model_failed")
        assert not await repo.record_run(
            cid, job_id, agent_run_id=uuid.uuid4(), cost_usd=Decimal("0"), served_provider=None
        )
        assert not await repo.reject(cid, None)
        assert not await repo.reopen(cid)
        assert not await repo.mark_accepted(cid, src, None)
        assert await repo.fail_for_source(src) == []
        assert not await repo.delete(cid)
        assert not await uow.images.demote_main(src, 1)
    row = admin_conn.execute(
        "SELECT status, job_id FROM image_candidates WHERE id = %s", [cid]
    ).fetchone()
    assert row == ("queued", job_id)


async def test_iso_candidate_write_other_tenant_refused(world: World, admin_conn: Conn) -> None:
    from app.db.guard import CrossTenantQueryError
    from app.repositories.image_candidates import ImageCandidateRepository, NewCandidate
    from app.repositories.transactions import unit_of_work

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    p, src, _ = _candidate(admin_conn, bharat)
    new = NewCandidate(
        id=uuid.uuid4(), product_id=p, colourway_id=None, source_image_id=src, kind="fix",
        source_hash="f" * 64, generator_key="fix/v1", created_by=None,
    )  # fmt: skip
    async with unit_of_work(acme) as session:
        with pytest.raises(CrossTenantQueryError):
            await ImageCandidateRepository(session, bharat).create_or_get(new)
