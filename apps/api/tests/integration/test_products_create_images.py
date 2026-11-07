"""SI1 T14 (AC8, AC18; PLAN D3, CP8): the minted-id upload URL and `POST /products` with
`image_uploads`, against an in-memory store. Run in the Docker batch
(`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.services.image_keys import image_object_key, parse_image_key

from .conftest import LOCAL_HOSTS
from .images_support import UPLOAD_URL, FakeStorage, images_db, picture, product, use_storage
from .jobs_support import JobWorld, as_member, count, insert_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
PRODUCTS = "/api/v1/products"
JPEG = picture(40, 40)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


def _op(jw: JobWorld) -> dict[str, str]:
    return as_member(jw, "operator@acme.test")


def _key(tenant: uuid.UUID, product_id: uuid.UUID) -> str:
    return image_object_key(tenant, product_id, uuid.uuid4())


async def _put(store: FakeStorage, key: str, data: bytes = JPEG) -> None:
    await store.put("images", key, data, "image/jpeg")


def _body(product_id: uuid.UUID, uploads: list[dict[str, Any]], sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "title": "Tee",
        "category": "t_shirts",
        "id": str(product_id),
        "image_uploads": uploads,
    }


def _rows(jw: JobWorld, product_id: uuid.UUID) -> tuple[int, int, int]:
    products = count(jw.conn, "SELECT count(*) FROM products WHERE id = %s", [product_id])
    images = count(jw.conn, "SELECT count(*) FROM images WHERE product_id = %s", [product_id])
    jobs = count(
        jw.conn,
        "SELECT count(*) FROM job_runs WHERE kind = 'image_compliance' AND entity_id IN "
        "(SELECT id FROM images WHERE product_id = %s)",
        [product_id],
    )
    return products, images, jobs


# ---- the upload URL ----


async def test_upload_url_without_product_mints_an_id(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    response = await api.post(
        UPLOAD_URL,
        headers=_op(images_db),
        json={"slot": "size_chart", "content_type": "image/jpeg", "bytes": 5},
    )
    assert response.status_code == 200, response.text
    out = response.json()
    parsed = parse_image_key(out["storage_key"])
    assert parsed is not None
    assert parsed.tenant_id == images_db.tenant("acme")
    assert str(parsed.product_id) == out["product_id"]
    assert (
        count(images_db.conn, "SELECT count(*) FROM products WHERE id = %s", [parsed.product_id])
        == 0
    )


async def test_upload_url_with_product_unchanged(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    pid = product(images_db.conn, images_db.tenant("acme"))
    response = await api.post(
        UPLOAD_URL,
        headers=_op(images_db),
        json={"product_id": str(pid), "slot": "main", "content_type": "image/jpeg", "bytes": 5},
    )
    assert response.status_code == 200, response.text
    assert response.json()["product_id"] == str(pid)


# ---- create with uploads ----


async def test_create_with_two_keys(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    keys = [_key(tenant, pid), _key(tenant, pid)]
    for key in keys:
        await _put(store, key)
    uploads = [
        {"storage_key": keys[0], "slot": "main"},
        {"storage_key": keys[1], "slot": "secondary"},
    ]
    response = await api.post(
        PRODUCTS, json=_body(pid, uploads, "SI1-IMG-2"), headers=_op(images_db)
    )
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["id"] == str(pid)
    assert out["images"] == keys
    assert [d["slot"] for d in out["image_details"]] == ["main", "secondary"]
    assert _rows(images_db, pid) == (1, 2, 2)


async def test_foreign_key_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    pid = uuid.uuid4()
    foreign = _key(images_db.tenant("bharat"), pid)
    await _put(store, foreign)
    missing = _key(images_db.tenant("acme"), pid)
    a = await api.post(
        PRODUCTS,
        json=_body(pid, [{"storage_key": foreign, "slot": "main"}], "SI1-IMG-F"),
        headers=_op(images_db),
    )
    b = await api.post(
        PRODUCTS,
        json=_body(pid, [{"storage_key": missing, "slot": "main"}], "SI1-IMG-F"),
        headers=_op(images_db),
    )
    assert a.status_code == b.status_code == 404
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    assert a.json()["error"]["details"] == b.json()["error"]["details"]
    assert _rows(images_db, pid) == (0, 0, 0)


async def test_key_under_another_id_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    pid, other = uuid.uuid4(), uuid.uuid4()
    key = _key(images_db.tenant("acme"), other)
    await _put(store, key)
    response = await api.post(
        PRODUCTS,
        json=_body(pid, [{"storage_key": key, "slot": "main"}], "SI1-IMG-O"),
        headers=_op(images_db),
    )
    assert response.status_code == 404


async def test_registered_key_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    key = _key(tenant, pid)
    await _put(store, key)
    first = await api.post(
        PRODUCTS,
        json=_body(pid, [{"storage_key": key, "slot": "main"}], "SI1-IMG-R1"),
        headers=_op(images_db),
    )
    assert first.status_code == 201
    pid2 = uuid.uuid4()
    again = await api.post(
        PRODUCTS,
        json=_body(pid2, [{"storage_key": key, "slot": "main"}], "SI1-IMG-R2"),
        headers=_op(images_db),
    )
    assert again.status_code == 404


async def test_existing_and_soft_deleted_id_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    live = product(images_db.conn, tenant)
    gone = product(images_db.conn, tenant)
    images_db.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [gone])
    for i, pid in enumerate((live, gone)):
        key = _key(tenant, pid)
        await _put(store, key)
        response = await api.post(
            PRODUCTS,
            json=_body(pid, [{"storage_key": key, "slot": "main"}], f"SI1-IMG-X{i}"),
            headers=_op(images_db),
        )
        assert response.status_code == 404, response.text


async def test_two_mains_and_21_keys_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    keys = [_key(tenant, pid) for _ in range(21)]
    for key in keys[:2]:
        await _put(store, key)
    two = [{"storage_key": k, "slot": "main"} for k in keys[:2]]
    response = await api.post(PRODUCTS, json=_body(pid, two, "SI1-IMG-M"), headers=_op(images_db))
    assert response.status_code == 422
    many = [{"storage_key": k, "slot": "secondary"} for k in keys]
    response = await api.post(PRODUCTS, json=_body(pid, many, "SI1-IMG-21"), headers=_op(images_db))
    assert response.status_code == 422


def _live_jobs(jw: JobWorld, tenant: uuid.UUID, n: int) -> None:
    for _ in range(n):
        insert_job(
            jw.conn, tenant, kind="image_compliance", entity_type="image", entity_id=uuid.uuid4()
        )


async def test_create_at_cap_429(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    _live_jobs(images_db, tenant, 20)
    key = _key(tenant, pid)
    await _put(store, key)
    response = await api.post(
        PRODUCTS,
        json=_body(pid, [{"storage_key": key, "slot": "main"}], "SI1-IMG-CAP"),
        headers=_op(images_db),
    )
    assert response.status_code == 429
    assert _rows(images_db, pid) == (0, 0, 0)


async def test_create_partial_cap_overflow_429(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    _live_jobs(images_db, tenant, 15)
    keys = [_key(tenant, pid) for _ in range(8)]
    for key in keys:
        await _put(store, key)
    uploads = [{"storage_key": k, "slot": "secondary"} for k in keys]
    response = await api.post(
        PRODUCTS, json=_body(pid, uploads, "SI1-IMG-OV"), headers=_op(images_db)
    )
    assert response.status_code == 429
    assert _rows(images_db, pid) == (0, 0, 0)
    pid2 = uuid.uuid4()
    five = [_key(tenant, pid2) for _ in range(5)]
    for key in five:
        await _put(store, key)
    ok = await api.post(
        PRODUCTS,
        json=_body(pid2, [{"storage_key": k, "slot": "secondary"} for k in five], "SI1-IMG-OK"),
        headers=_op(images_db),
    )
    assert ok.status_code == 201, ok.text
