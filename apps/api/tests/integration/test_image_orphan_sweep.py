"""S17b T15 (AC12; PLAN D9; CP3, CP11): the image orphan sweep in `retention_daily`, against the
compose MinIO: old unregistered image objects go, registered (also soft-deleted) rows' objects,
young objects, non-image keys and other tenants' objects stay; every page is reached."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from app.adapters.storage import ObjectStorage, StorageUnavailable, get_storage, images_bucket
from app.services.image_keys import image_object_key
from app.workers import periodic
from app.workers.periodic import _sweep_start, retention_daily, sweep_image_orphans

from .conftest import LOCAL_HOSTS
from .images_support import image_row, images_db, product
from .jobs_support import JobWorld, count, insert_job
from .products_support import statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
LATER = datetime.now(UTC) + timedelta(hours=25)  # every object put now is "older than 24 h"


async def puts(keys: list[str]) -> None:
    storage = get_storage()
    sem = asyncio.Semaphore(32)

    async def one(k: str) -> None:
        async with sem:
            await storage.put(images_bucket(), k, b"x", "application/octet-stream")

    await asyncio.gather(*(one(k) for k in keys))


async def exists(key: str) -> bool:
    return await get_storage().head(images_bucket(), key) is not None


async def listed(tenant: uuid.UUID) -> set[str]:
    """Every object key under the tenant prefix (paged listing, not one head per key)."""
    found: set[str] = set()
    far = datetime.now(UTC) + timedelta(days=3650)
    async for page in get_storage().iter_older_than(images_bucket(), f"{tenant}/", far):
        found.update(o.key for o in page)
    return found


def orphan_key(jw: JobWorld, slug: str = "acme", product_id: uuid.UUID | None = None) -> str:
    return image_object_key(jw.tenant(slug), product_id or uuid.uuid4(), uuid.uuid4())


def registered(jw: JobWorld, slug: str = "acme", **cols: Any) -> str:
    tenant = jw.tenant(slug)
    image_id = image_row(jw.conn, tenant, product(jw.conn, tenant), **cols)
    row = jw.conn.execute("SELECT storage_key FROM images WHERE id = %s", [image_id]).fetchone()
    assert row is not None
    return str(row[0])


async def test_image_orphan_sweep_deletes_old_unregistered(images_db: JobWorld) -> None:
    key = orphan_key(images_db)
    await puts([key])
    assert await sweep_image_orphans(LATER) == 1
    assert not await exists(key)


async def test_image_orphan_sweep_keeps_young(images_db: JobWorld) -> None:
    key = orphan_key(images_db)
    await puts([key])
    assert await sweep_image_orphans(datetime.now(UTC)) == 0
    assert await exists(key)


async def test_image_orphan_sweep_keeps_registered(images_db: JobWorld) -> None:
    key = registered(images_db)
    await puts([key])
    await sweep_image_orphans(LATER)
    assert await exists(key)


async def test_image_orphan_sweep_keeps_soft_deleted_rows_object(images_db: JobWorld) -> None:
    key = registered(images_db, deleted_at=datetime.now(UTC))
    await puts([key])
    await sweep_image_orphans(LATER)
    assert await exists(key)


async def test_image_orphan_sweep_never_crosses_tenants(images_db: JobWorld) -> None:
    """A has a row for key K; an old object at B's prefix with K's product and image ids and no
    B row is deleted in B's pass, and A's object at K stays."""
    k = registered(images_db, "acme")
    acme, bharat = images_db.tenant("acme"), images_db.tenant("bharat")
    twin = k.replace(str(acme), str(bharat), 1)
    await puts([k, twin])
    assert await sweep_image_orphans(LATER) == 1
    assert await exists(k) and not await exists(twin)


async def test_image_orphan_sweep_ignores_non_image_keys(images_db: JobWorld) -> None:
    tenant = images_db.tenant("acme")
    keys = [f"{tenant}/logo.png", f"{tenant}/imports/{uuid.uuid4()}.csv"]
    await puts(keys)
    assert await sweep_image_orphans(LATER) == 0
    assert all([await exists(k) for k in keys])


async def test_image_orphan_sweep_reaches_orphans_after_1000_registered(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    # start below every key, so the 1,200 registered keys fill the first page and a half
    monkeypatch.setattr(periodic, "_sweep_start", lambda t, _d: f"{t}/0")
    tenant = images_db.tenant("acme")
    low = product(images_db.conn, tenant, id=uuid.UUID("00000000-0000-4000-8000-000000000001"))
    high = uuid.UUID("ffffffff-0000-4000-8000-000000000001")
    rows = images_db.conn.execute(
        "INSERT INTO images (tenant_id, product_id, storage_key, slot) "
        "SELECT %s, %s, %s || '/' || %s || '/images/' || gen_random_uuid(), 'secondary' "
        "FROM generate_series(1, 1200) RETURNING storage_key",
        [tenant, low, str(tenant), str(low)],
    ).fetchall()
    kept = [str(r[0]) for r in rows]
    orphans = [orphan_key(images_db, product_id=high) for _ in range(5)]
    await puts(kept + orphans)
    assert await sweep_image_orphans(LATER) == 5
    assert not any([await exists(k) for k in orphans])
    assert all([await exists(k) for k in kept[:20] + kept[-20:]])


async def test_image_orphan_sweep_drains_beyond_one_page(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [orphan_key(images_db) for _ in range(2500)]
    await puts(keys)
    calls: list[int] = []
    real = ObjectStorage.delete_many

    async def spy(self: ObjectStorage, bucket: str, batch: Any) -> None:
        calls.append(len(batch))
        await real(self, bucket, batch)

    monkeypatch.setattr(ObjectStorage, "delete_many", spy)
    assert await sweep_image_orphans(LATER) == 2500
    assert sum(calls) == 2500 and len(calls) >= 3
    assert await listed(images_db.tenant("acme")) == set()


async def test_image_orphan_sweep_budget_stop_logs_more_pending(
    images_db: JobWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    keys = [orphan_key(images_db) for _ in range(2500)]
    await puts(keys)
    monkeypatch.setattr(periodic, "SWEEP_RUN_BUDGET_S", 0)
    caplog.set_level(logging.INFO)
    capfd.readouterr()
    deleted = await sweep_image_orphans(LATER)
    assert 0 <= deleted <= 1000
    assert "image_orphans_more_pending" in caplog.text + "".join(capfd.readouterr())
    left = set(keys) & await listed(images_db.tenant("acme"))
    assert len(left) == 2500 - deleted


def _low_start_dates(jw: JobWorld, n: int = 2) -> list[date]:
    """Run dates (after every object here is a day old) that visit acme first and whose start
    key falls in the first half of the id space, so the first page holds acme's orphans."""
    tenant = jw.tenant("acme")
    tenants = sorted([jw.tenant("acme"), jw.tenant("bharat")])
    found: list[date] = []
    day = (datetime.now(UTC) + timedelta(days=2)).date()
    while len(found) < n:
        shift = day.toordinal() % len(tenants)
        first = (tenants[shift:] + tenants[:shift])[0]
        if first == tenant and _sweep_start(tenant, day).split("/", 1)[1][0] in "01234567":
            found.append(day)
        day += timedelta(days=1)
    return found


async def test_image_orphan_sweep_rotated_start_reaches_other_ranges(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [orphan_key(images_db) for _ in range(3000)]
    await puts(keys)
    monkeypatch.setattr(periodic, "SWEEP_RUN_BUDGET_S", 0)
    gone: list[set[str]] = []
    for day in _low_start_dates(images_db):
        before = await listed(images_db.tenant("acme"))
        await sweep_image_orphans(datetime(day.year, day.month, day.day, 3, tzinfo=UTC))
        gone.append(before - await listed(images_db.tenant("acme")))
    assert gone[0] and gone[1] and gone[0] != gone[1]


async def test_image_orphan_sweep_one_registered_keys_query_per_page(images_db: JobWorld) -> None:
    keys = [orphan_key(images_db) for _ in range(1500)]
    await puts(keys)
    with statements() as seen:
        assert await sweep_image_orphans(LATER) == 1500
    queries = [s for s in seen if "FROM images" in s]
    assert 2 <= len(queries) <= 4  # one per non-empty page (1,000-key pages, wrap included)


async def test_image_orphan_sweep_storage_error_does_not_stop_other_steps(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = images_db.tenant("acme")
    old = insert_job(
        images_db.conn, tenant, status="succeeded", attempt=1,
        queued_at=datetime.now(UTC) - timedelta(days=101),
        started_at=datetime.now(UTC) - timedelta(days=101),
        finished_at=datetime.now(UTC) - timedelta(days=100),
    )  # fmt: skip

    class Down:
        async def iter_older_than(self, *_a: Any, **_k: Any) -> Any:
            raise StorageUnavailable("down")
            yield  # pragma: no cover

    monkeypatch.setattr(periodic, "get_storage", lambda: Down())
    totals = await retention_daily()
    assert totals["image_orphans"] == 0 and totals["job_runs"] == 1
    assert count(images_db.conn, "SELECT count(*) FROM job_runs WHERE id = %s", [old]) == 0
