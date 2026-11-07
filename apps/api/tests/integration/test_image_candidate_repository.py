"""SI7 T9 (AC4, AC9, AC10, AC12, AC16): `ImageCandidateRepository` and the image additions as
app_rw, against a committed world."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.repositories.budget import image_edit_spend
from app.repositories.image_candidates import NewCandidate
from app.repositories.images import NewImage
from app.repositories.uow import tenant_uow
from app.services.image_keys import image_object_key
from mkos_schemas.enums import ImageSlot

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import image_row, images_db, product
from .jobs_support import JobWorld

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

HASH = "a" * 64


def _new(product_id: uuid.UUID, source: uuid.UUID | None, **kw: Any) -> NewCandidate:
    return NewCandidate(
        id=kw.pop("id", uuid.uuid4()),
        product_id=product_id,
        colourway_id=None,
        source_image_id=source,
        kind=kw.pop("kind", "fix" if source else "size_chart"),
        source_hash=kw.pop("source_hash", HASH),
        generator_key=kw.pop("generator_key", "fix/v1"),
        created_by=None,
        **kw,
    )


def _world(jw: JobWorld, slug: str = "acme") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = jw.tenant(slug)
    p = product(jw.conn, tenant)
    return tenant, p, image_row(jw.conn, tenant, p, slot="main")


async def test_create_or_get_then_cached(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    async with tenant_uow(tenant) as uow:
        row, created = await uow.image_candidates.create_or_get(_new(p, src))
        assert created and row.status == "queued" and row.job_id is None
        again, created2 = await uow.image_candidates.create_or_get(_new(p, src))
        assert not created2 and again.id == row.id
        assert (await uow.image_candidates.find_cached(p, "fix", HASH, "fix/v1")) is not None
        assert (await uow.image_candidates.find_cached(p, "fix", "b" * 64, "fix/v1")) is None


async def test_concurrent_creates_make_one_row(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)

    async def one() -> tuple[uuid.UUID, bool]:
        async with tenant_uow(tenant) as uow:
            row, created = await uow.image_candidates.create_or_get(_new(p, src))
            await asyncio.sleep(0.05)
            return row.id, created

    results = await asyncio.gather(one(), one())
    assert len({r[0] for r in results}) == 1
    assert sorted(r[1] for r in results) == [False, True]
    count = images_db.conn.execute("SELECT count(*) FROM image_candidates").fetchone()
    assert count == (1,)


async def test_set_job_only_once_and_fenced_outcomes(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    job, other = uuid.uuid4(), uuid.uuid4()
    async with tenant_uow(tenant) as uow:
        repo = uow.image_candidates
        row, _ = await repo.create_or_get(_new(p, src))
        assert await repo.set_job(row.id, job)
        assert not await repo.set_job(row.id, other)
        assert not await repo.mark_ready(row.id, other, storage_key="k", width=1, height=1)
        assert not await repo.mark_failed(row.id, other, "model_failed")
        assert await repo.mark_ready(row.id, job, storage_key="k", width=1200, height=1200)
        assert not await repo.mark_failed(row.id, job, "model_failed")  # no longer queued
        found = await repo.get(row.id)
    assert found is not None and found.job_id == job and found.status == "ready"
    assert (found.width_px, found.mime_type) == (1200, "image/jpeg")


async def test_record_run_is_fenced_on_the_job(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    job = uuid.uuid4()
    run = f.agent_run(images_db.conn, tenant, agent_name="image", trigger="vision")
    async with tenant_uow(tenant) as uow:
        repo = uow.image_candidates
        row, _ = await repo.create_or_get(_new(p, src, kind="white_background"))
        await repo.set_job(row.id, job)
        cost = Decimal("0.039")
        assert not await repo.record_run(
            row.id, uuid.uuid4(), agent_run_id=run, cost_usd=cost, served_provider="Google"
        )
        assert await repo.record_run(
            row.id, job, agent_run_id=run, cost_usd=cost, served_provider="Google"
        )
        found = await repo.get(row.id)
    assert found is not None and found.agent_run_id == run and found.served_provider == "Google"


async def test_reject_reopen_accept(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    job = uuid.uuid4()
    accepted_image = image_row(images_db.conn, tenant, p, slot="secondary")
    async with tenant_uow(tenant) as uow:
        repo = uow.image_candidates
        row, _ = await repo.create_or_get(_new(p, src))
        await repo.set_job(row.id, job)
        assert not await repo.reject(row.id, None)  # queued
        await repo.mark_ready(row.id, job, storage_key="k", width=1, height=1)
        assert await repo.reject(row.id, None)
        assert await repo.reopen(row.id)
        assert await repo.mark_accepted(row.id, accepted_image, None)
        assert not await repo.reopen(row.id)
        assert not await repo.reject(row.id, None)
        found = await repo.get(row.id)
    assert found is not None and found.status == "accepted"
    assert found.accepted_image_id == accepted_image


async def test_demote_main_keeps_one_main(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    new_id = uuid.uuid4()
    async with tenant_uow(tenant) as uow:
        assert await uow.images.demote_main(src, 3)
        assert not await uow.images.demote_main(src, 4)  # no longer the main
        made = await uow.images.create(
            NewImage(
                id=new_id,
                product_id=p,
                colourway_id=None,
                storage_key=image_object_key(tenant, p, new_id),
                slot=ImageSlot.MAIN,
                bytes=10,
                position=0,
                origin="rendered",
                parent_image_id=src,
                candidate_id=uuid.uuid4(),
            )
        )
        assert made is not None and made.origin == "rendered" and made.parent_image_id == src
    rows = images_db.conn.execute(
        "SELECT id, slot, position FROM images WHERE product_id = %s ORDER BY slot", [p]
    ).fetchall()
    assert rows == [(new_id, "main", 0), (src, "secondary", 3)]


async def test_count_ai_today(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    today = datetime.now(UTC).date()
    run = f.agent_run(images_db.conn, tenant, agent_name="image", trigger="vision")
    async with tenant_uow(tenant) as uow:
        repo = uow.image_candidates
        queued, _ = await repo.create_or_get(_new(p, src, kind="white_background"))
        reached, _ = await repo.create_or_get(
            _new(p, src, kind="white_background", source_hash="b" * 64)
        )
        await repo.set_job(reached.id, j := uuid.uuid4())
        await repo.record_run(
            reached.id, j, agent_run_id=run, cost_usd=Decimal("0.04"), served_provider=None
        )
        await repo.mark_failed(reached.id, j, "model_failed")
        await repo.create_or_get(
            _new(
                p,
                src,
                kind="white_background",
                source_hash="c" * 64,
                status="failed",
                failure_reason="budget",
            )
        )
        await repo.create_or_get(_new(p, src, source_hash="d" * 64))  # a fix: never counted
    yesterday = datetime.now(UTC) - timedelta(days=1)
    f.image_candidate(images_db.conn, tenant, p, src, kind="white_background", created_at=yesterday)
    async with tenant_uow(tenant) as uow:
        assert await uow.image_candidates.count_ai_today(today) == 2
        assert await uow.image_candidates.count_ai_today(today, exclude_id=queued.id) == 1


async def test_image_edit_spend_counts_edits_only(images_db: JobWorld) -> None:
    from app.db.session import platform_scope

    tenant, _, _ = _world(images_db)
    now = datetime.now(UTC)
    for prompt, cost in (("image_edit/white_bg/v1", "0.039"), ("photo_facts/v1", "0.004")):
        f.agent_run(
            images_db.conn,
            tenant,
            agent_name="image",
            trigger="vision",
            prompt_version=prompt,
            cost_usd=cost,
            started_at=now,
        )
    f.agent_run(images_db.conn, tenant, cost_usd="1.0", started_at=now)  # generation
    f.agent_run(
        images_db.conn,
        tenant,
        agent_name="image",
        trigger="vision",
        prompt_version="image_edit/white_bg/v1",
        cost_usd="0.039",
        started_at=now - timedelta(days=1),
    )
    async with platform_scope() as session:
        assert await image_edit_spend(session, now.date()) == Decimal("0.039")


async def test_expired_and_delete(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    old = datetime.now(UTC) - timedelta(days=31)
    stale = f.image_candidate(
        images_db.conn, tenant, p, src, created_at=old, status="failed", failure_reason="fidelity"
    )
    kept = f.image_candidate(
        images_db.conn,
        tenant,
        p,
        src,
        created_at=old,
        status="accepted",
        storage_key="k",
        accepted_image_id=src,
    )
    async with tenant_uow(tenant) as uow:
        repo = uow.image_candidates
        found = await repo.expired(datetime.now(UTC) - timedelta(days=30), 500)
        assert [c.id for c in found] == [stale]
        assert await repo.delete(stale)
        assert not await repo.delete(kept)


async def test_fail_for_source_returns_keys(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    ready = f.image_candidate(images_db.conn, tenant, p, src, status="ready", storage_key="k1")
    async with tenant_uow(tenant) as uow:
        keys = await uow.image_candidates.fail_for_source(src)
        row = await uow.image_candidates.get(ready)
    assert keys == ["k1"]
    assert row is not None and (row.status, row.failure_reason) == ("failed", "source_unusable")


async def test_list_for_product_newest_first(images_db: JobWorld) -> None:
    tenant, p, src = _world(images_db)
    base = datetime.now(UTC)
    ids = [
        f.image_candidate(images_db.conn, tenant, p, src, created_at=base - timedelta(minutes=i))
        for i in range(3)
    ]
    async with tenant_uow(tenant) as uow:
        rows = await uow.image_candidates.list_for_product(p)
        with pytest.raises(ValueError):
            await uow.image_candidates.list_for_product(p, limit=51)
    assert [r.id for r in rows] == ids
