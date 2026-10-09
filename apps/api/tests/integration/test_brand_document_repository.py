"""S16 T9 (AC1, AC6; DATABASE.md:1055): `BrandDocumentRepository` and the S16 `job_runs` helpers,
as app_rw."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from .auth_support import World
from .brand_support import Conn, doc_row, new_document
from .conftest import LOCAL_HOSTS
from .jobs_support import insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
REPORT = {"status": "failed", "error_code": "MODEL_UNAVAILABLE", "errors": []}


def _acme(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    return world.tenant("acme"), world.user("owner@acme.test")


async def test_create_then_get_live(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        doc = await uow.brand_documents.get_live(values["id"])
        by_hash = await uow.brand_documents.get_live_by_hash(values["content_hash"])
    assert doc is not None and by_hash is not None and doc.id == by_hash.id
    assert (doc.status, doc.chunk_count, doc.job_id) == ("uploaded", 0, values["job_id"])


async def test_live_hash_unique(products_world: World, auth_db: Conn) -> None:
    from app.core.errors import ConflictError

    tenant, owner = _acme(products_world)
    first = await new_document(tenant, owner)
    with pytest.raises(ConflictError):
        await new_document(tenant, owner, content_hash=first["content_hash"])


async def test_deleted_hash_reusable(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    first = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        assert await uow.brand_documents.soft_delete(first["id"])
    second = await new_document(tenant, owner, content_hash=first["content_hash"])
    assert doc_row(auth_db, second["id"])["deleted_at"] is None


async def test_claim_fenced_by_job(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        assert await uow.brand_documents.claim_for_job(values["id"], uuid.uuid4()) is None
        key = await uow.brand_documents.claim_for_job(values["id"], values["job_id"])
    assert key == values["storage_key"]
    assert doc_row(auth_db, values["id"])["status"] == "processing"


async def test_claim_skips_deleted(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        await uow.brand_documents.soft_delete(values["id"])
        assert await uow.brand_documents.claim_for_job(values["id"], values["job_id"]) is None


async def test_mark_failed_requires_report(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        with pytest.raises(ValueError, match="ErrorReport"):
            await uow.brand_documents.mark_failed(values["id"], values["job_id"], {})
        assert await uow.brand_documents.mark_failed(values["id"], values["job_id"], REPORT)
    row = doc_row(auth_db, values["id"])
    assert row["status"] == "failed" and row["error_report_json"] == REPORT


async def test_mark_failed_fenced_old_job(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        assert not await uow.brand_documents.mark_failed(values["id"], uuid.uuid4(), REPORT)
        assert not await uow.brand_documents.mark_ready(values["id"], uuid.uuid4(), 3)
    assert doc_row(auth_db, values["id"])["status"] == "uploaded"


async def test_requeue_only_failed(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    new_job = uuid.uuid4()
    async with tenant_uow(tenant) as uow:
        assert not await uow.brand_documents.requeue_failed(values["id"], new_job)
        await uow.brand_documents.mark_failed(values["id"], values["job_id"], REPORT)
        assert await uow.brand_documents.requeue_failed(values["id"], new_job)
    row = doc_row(auth_db, values["id"])
    assert (row["status"], row["job_id"], row["error_report_json"]) == ("uploaded", new_job, None)


async def test_soft_delete_twice_false(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        assert await uow.brand_documents.soft_delete(values["id"])
        assert not await uow.brand_documents.soft_delete(values["id"])
        assert await uow.brand_documents.get_live(values["id"]) is None


async def test_list_page_walk_live_only(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    ids = [(await new_document(tenant, owner))["id"] for _ in range(5)]
    async with tenant_uow(tenant) as uow:
        await uow.brand_documents.soft_delete(ids[2])
        seen: list[uuid.UUID] = []
        after = None
        while True:
            page = await uow.brand_documents.list_page(limit=2, after=after)
            seen += [d.id for d in page.items]
            if not page.has_more:
                break
            last = page.items[-1]
            after = (last.created_at, last.id)
    assert sorted(seen) == sorted(set(ids) - {ids[2]}) and len(seen) == 4


async def test_other_tenant_invisible(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    values = await new_document(tenant, owner)
    async with tenant_uow(products_world.tenant("bharat")) as uow:
        assert await uow.brand_documents.get_live(values["id"]) is None
        assert await uow.brand_documents.get_live_by_hash(values["content_hash"]) is None
        assert not await uow.brand_documents.soft_delete(values["id"])
        assert (await uow.brand_documents.list_page(limit=50)).items == []
    assert doc_row(auth_db, values["id"])["deleted_at"] is None


async def test_count_live_documents_excludes_deleted(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow

    tenant, owner = _acme(products_world)
    first = await new_document(tenant, owner)
    await new_document(tenant, owner)
    async with tenant_uow(tenant) as uow:
        await uow.brand_documents.soft_delete(first["id"])
        assert await uow.brand_documents.count_live_documents() == 1


def _job(
    conn: Conn, tenant: uuid.UUID, source: str, status: str = "queued", **over: object
) -> uuid.UUID:
    return insert_job(
        conn,
        tenant,
        kind="brand_embed",
        entity_type="brand_document",
        status=status,
        payload_json={"source": source},
        **over,
    )


async def test_count_live_counts_document_jobs_only(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    tenant, _ = _acme(products_world)
    _job(auth_db, tenant, "brand_document")
    _job(
        auth_db,
        tenant,
        "brand_document",
        status="running",
        attempt=1,
        lease_expires_at="2099-01-01T00:00:00Z",
        started_at="2026-10-01T00:00:00Z",
    )
    _job(auth_db, tenant, "brand_document", status="succeeded", finished_at="2026-10-01T00:00:00Z")
    _job(auth_db, tenant, "brand_profile", entity_id=tenant)
    _job(auth_db, products_world.tenant("bharat"), "brand_document")
    async with tenant_uow(tenant) as uow:
        assert await uow.job_runs.count_live(JobKind.BRAND_EMBED, source="brand_document") == 2
        assert await uow.job_runs.count_live(JobKind.BRAND_EMBED, source="brand_profile") == 1


async def test_has_queued_profile(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    tenant, _ = _acme(products_world)
    async with tenant_uow(tenant) as uow:
        assert not await uow.job_runs.has_queued(
            JobKind.BRAND_EMBED, entity_id=tenant, source="brand_profile"
        )
    _job(auth_db, tenant, "brand_profile", entity_id=tenant)
    async with tenant_uow(tenant) as uow:
        assert await uow.job_runs.has_queued(
            JobKind.BRAND_EMBED, entity_id=tenant, source="brand_profile"
        )


async def test_has_queued_ignores_document_jobs(products_world: World, auth_db: Conn) -> None:
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    tenant, _ = _acme(products_world)
    _job(auth_db, tenant, "brand_document", entity_id=tenant)
    async with tenant_uow(tenant) as uow:
        assert not await uow.job_runs.has_queued(
            JobKind.BRAND_EMBED, entity_id=tenant, source="brand_profile"
        )


async def test_has_queued_locks_row(products_world: World, auth_db: Conn) -> None:
    import os

    import psycopg
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    tenant, _ = _acme(products_world)
    job = _job(auth_db, tenant, "brand_profile", entity_id=tenant)
    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    order: list[str] = []

    def claim() -> None:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", [str(tenant)])
            conn.execute(
                "UPDATE job_runs SET status = status WHERE id = %s AND status = 'queued'", [job]
            )
            order.append("claim")

    async with tenant_uow(tenant) as uow:
        assert await uow.job_runs.has_queued(
            JobKind.BRAND_EMBED, entity_id=tenant, source="brand_profile"
        )
        waiter = asyncio.create_task(asyncio.to_thread(claim))
        await asyncio.sleep(0.5)
        assert order == []  # the claim-style UPDATE waits on the row lock
        order.append("commit")
    await waiter
    assert order == ["commit", "claim"]


async def test_profile_job_row_passes_entity_check(products_world: World, auth_db: Conn) -> None:
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from mkos_schemas.enums import JobKind

    tenant, owner = _acme(products_world)
    spec = EnqueueSpec(
        kind=JobKind.BRAND_EMBED,
        entity_type="brand_document",
        entity_id=tenant,
        created_by=owner,
        payload={"source": "brand_profile"},
    )
    async with tenant_uow(tenant, user_id=owner) as uow:
        job = (await uow.job_runs.enqueue(spec)).job
    row = auth_db.execute(
        "SELECT entity_type, entity_id, payload_json FROM job_runs WHERE id = %s", [job.id]
    ).fetchone()
    assert row == ("brand_document", tenant, {"source": "brand_profile"})
