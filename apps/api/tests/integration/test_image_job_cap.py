"""S17b T10a (PLAN D15; AC16): image jobs carry the procrastinate lock `image_compliance` (one
runs at a time; waiting ones stay unfetched and hold no `default` slot), and a tenant has at most
20 live image jobs (429 `RATE_LIMITED` past it, nothing stored)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import tenant_uow
from app.services import image_runner
from app.services import images as svc
from app.services.image_keys import image_object_key
from app.workers import wrapper
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome
from mkos_schemas.enums import JobKind

from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    headers,
    images_db,
    picture,
    product,
    register_key,
    upload_file,
    use_storage,
)
from .jobs_support import JobWorld, count, eventually, insert_job, override_body, running_worker

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db", "override_body"]
JPEG = picture(64, 64)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


def live_jobs(jw: JobWorld, tenant: uuid.UUID, n: int) -> list[uuid.UUID]:
    return [
        insert_job(
            jw.conn, tenant, kind="image_compliance", entity_type="image", entity_id=uuid.uuid4()
        )
        for _ in range(n)
    ]


def put_key(jw: JobWorld, store: FakeStorage, p: uuid.UUID, slug: str = "acme") -> str:
    key = image_object_key(jw.tenant(slug), p, uuid.uuid4())
    store.objects[key] = (JPEG, "image/jpeg", datetime.now(UTC))
    return key


async def test_enqueue_passes_lock(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    body = (await upload_file(api, headers(images_db), p, JPEG)).json()
    async with tenant_uow(acme) as uow:
        bulk = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=uuid.uuid4(),
                created_by=None,
            )
        )
    locks: dict[Any, Any] = dict(
        images_db.conn.execute(
            "SELECT j.id, q.lock FROM job_runs j JOIN procrastinate_jobs q ON q.id = j.queue_job_id::bigint"
        ).fetchall()
    )
    assert locks[uuid.UUID(body["job_id"])] == "image_compliance"
    assert locks[bulk.job.id] is None


async def test_image_live_cap_per_tenant_429(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    jobs = live_jobs(images_db, acme, 20)
    hdrs = headers(images_db)
    key = put_key(images_db, store, p)
    response = await register_key(api, hdrs, p, key)
    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "RATE_LIMITED"
    assert count(images_db.conn, "SELECT count(*) FROM images") == 0
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() WHERE id = %s", [jobs[0]]
    )
    again = await register_key(api, hdrs, p, key)  # the same Idempotency-Key works now
    assert again.status_code == 202, again.text


async def test_image_cap_multipart_precheck_stores_nothing(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    live_jobs(images_db, acme, 20)
    response = await upload_file(api, headers(images_db), p, JPEG)
    assert response.status_code == 429
    assert store.objects == {} and "put" not in store.calls


async def test_image_cap_concurrent_never_exceeds_n(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    products = [product(images_db.conn, acme) for _ in range(40)]
    keys = [put_key(images_db, store, p) for p in products]
    results = await asyncio.gather(
        *(
            register_key(api, headers(images_db), p, k, slot="secondary")
            for p, k in zip(products, keys, strict=True)
        )
    )
    codes = sorted(r.status_code for r in results)
    assert codes.count(202) == 20 and codes.count(429) == 20, codes
    assert count(images_db.conn, "SELECT count(*) FROM images") == 20
    assert (
        count(images_db.conn, "SELECT count(*) FROM job_runs WHERE kind = 'image_compliance'") == 20
    )


async def test_image_cap_other_tenant_unaffected(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    live_jobs(images_db, images_db.tenant("acme"), 20)
    p = product(images_db.conn, images_db.tenant("bharat"))
    response = await upload_file(api, headers(images_db, "operator@bharat.test"), p, JPEG)
    assert response.status_code == 202


async def test_image_cap_replay_and_reregister_not_counted(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = put_key(images_db, store, p)
    hdrs = headers(images_db)
    first = await register_key(api, hdrs, p, key)
    assert first.status_code == 202
    live_jobs(images_db, acme, 19)  # 20 live now
    replay = await register_key(api, hdrs, p, key)
    reregister = await register_key(api, headers(images_db), p, key)
    assert replay.status_code == reregister.status_code == 202
    assert replay.json()["image_id"] == reregister.json()["image_id"] == first.json()["image_id"]


async def _image_jobs(jw: JobWorld, store: FakeStorage, n: int) -> list[uuid.UUID]:
    from .images_support import image_db_row, image_row

    acme = jw.tenant("acme")
    out = []
    for _ in range(n):
        p = product(jw.conn, acme)
        image_id = image_row(jw.conn, acme, p, slot="main")
        store.objects[image_db_row(jw.conn, image_id)["storage_key"]] = (
            JPEG,
            "image/jpeg",
            datetime.now(UTC),
        )
        out.append(await _enqueue_locked(acme, image_id))
    return out


async def _enqueue_locked(tenant: uuid.UUID, image_id: uuid.UUID) -> uuid.UUID:
    async with tenant_uow(tenant) as uow:
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type="image",
                entity_id=image_id,
                created_by=None,
                payload={"image_id": str(image_id)},
                lock=svc.IMAGE_JOB_LOCK,
            )
        )
    return enqueued.job.id


def _states(jw: JobWorld) -> dict[str, list[str]]:
    rows = jw.conn.execute(
        "SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for task, status in rows:
        out.setdefault(task, []).append(status)
    return out


async def test_image_jobs_hold_at_most_one_default_slot(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch, override_body: Any
) -> None:
    release = asyncio.Event()
    real = image_runner.run_checks_isolated

    async def blocked(*a: Any, **k: Any) -> Any:
        await release.wait()
        return await real(*a, **k)

    monkeypatch.setattr(image_runner, "run_checks_isolated", blocked)
    imported = asyncio.Event()

    async def bulk(_ctx: JobContext) -> Outcome:
        imported.set()
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, bulk)
    await _image_jobs(images_db, store, 4)
    async with tenant_uow(images_db.tenant("acme")) as uow:
        await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=uuid.uuid4(),
                created_by=None,
            )
        )
    async with running_worker(concurrency=4):
        await asyncio.wait_for(imported.wait(), timeout=15)
        await eventually(lambda: _states(images_db).get("image_compliance", []).count("doing") == 1)
        states = _states(images_db)["image_compliance"]
        assert states.count("doing") == 1 and states.count("todo") == 3, states
        release.set()
        await eventually(
            lambda: _states(images_db)["image_compliance"].count("succeeded") == 4, timeout_s=60
        )


async def test_waiting_image_job_timeout_not_started(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job waiting `todo` behind the lock has not started its timeout: with a 2 s body timeout
    and three 1.2 s jobs in a row, the third waited longer than 2 s and still succeeds."""
    policies = dict(POLICIES)
    policies[JobKind.IMAGE_COMPLIANCE] = dataclasses.replace(
        policies[JobKind.IMAGE_COMPLIANCE], timeout_s=2
    )
    monkeypatch.setattr(wrapper, "POLICIES", policies)
    real = image_runner.run_checks_isolated

    async def slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(1.2)
        return await real(*a, **k)

    monkeypatch.setattr(image_runner, "run_checks_isolated", slow)
    jobs = await _image_jobs(images_db, store, 3)
    async with running_worker(concurrency=4):
        await eventually(
            lambda: _states(images_db).get("image_compliance", []).count("succeeded") == 3,
            timeout_s=60,
        )
    statuses = [
        images_db.conn.execute("SELECT status FROM job_runs WHERE id = %s", [j]).fetchone()[0]  # type: ignore[index]
        for j in jobs
    ]
    assert statuses == ["succeeded"] * 3
