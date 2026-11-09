"""SI14 T9 (AC3, AC6): a product without a category, end to end. It is created and read with
`category: null`, a listing refuses it at `category`, its photos and size-chart read run with the
category-wide rules only, and PATCH sets the category with its provenance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .images_support import (
    FakeStorage,
    enqueue_image_job,
    image_db_row,
    image_row,
    images_db,
    picture,
    run_image_job,
    use_storage,
)
from .jobs_support import JobWorld, job_row
from .size_chart_support import chart_image, enqueue_read, read_state, use_read_storage

__all__ = ["gen_world", "images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
PRODUCTS = "/api/v1/products"


async def _create(api: httpx.AsyncClient, world: GenWorld, **body: Any) -> httpx.Response:
    payload = {"sku": f"NOCAT-{uuid.uuid4().hex[:8]}", "title": "Cotton crew tee", **body}
    return await api.post(PRODUCTS, headers=world.headers("operator@acme.test"), json=payload)


async def test_create_and_read_without_category(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    created = await _create(api, gen_world)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["category"] is None
    product_id = body["id"]
    row = gen_world.conn.execute(
        "SELECT category, provenance ? 'category' FROM products WHERE id = %s", [product_id]
    ).fetchone()
    assert row == (None, False)
    read = await api.get(
        f"{PRODUCTS}/{product_id}", headers=gen_world.headers("operator@acme.test")
    )
    assert read.status_code == 200
    assert read.json()["category"] is None
    assert read.json()["suggestions"]["hsn_code"] is None
    listed = await api.get(PRODUCTS, headers=gen_world.headers("operator@acme.test"))
    assert listed.status_code == 200
    mine = [p for p in listed.json()["data"] if p["id"] == product_id]
    assert mine and mine[0]["category"] is None


async def test_generate_refuses_a_product_without_category(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product_id = (await _create(api, gen_world)).json()["id"]
    response = await api.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers("operator@acme.test"),
        json={"product_id": product_id, "marketplace": "amazon"},
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "VALIDATION_FAILED"
    assert error["details"][0]["loc"] == ["body", "category"]
    assert gen_world.count("listings", "product_id = %s", [product_id]) == 0


async def test_patch_sets_the_category_and_its_source(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    product_id = (await _create(api, gen_world)).json()["id"]
    hdrs = gen_world.headers("operator@acme.test")
    etag = (await api.get(f"{PRODUCTS}/{product_id}", headers=hdrs)).headers["ETag"]
    patched = await api.patch(
        f"{PRODUCTS}/{product_id}",
        headers={**hdrs, "If-Match": etag},
        json={"category": "ethnic_wear_kurta_sets"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["category"] == "ethnic_wear_kurta_sets"
    row = gen_world.conn.execute(
        "SELECT category, provenance -> 'category' ->> 'source' FROM products WHERE id = %s",
        [product_id],
    ).fetchone()
    assert row == ("ethnic_wear_kurta_sets", "seller")


def _null_product(jw: JobWorld, kind: str = "standalone") -> uuid.UUID:
    return f.product(
        jw.conn, jw.tenant("acme"), kind, category=None, provenance={"title": f.fact()}
    )


async def test_photo_checks_run_with_category_wide_rules(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStorage()
    use_storage(monkeypatch, store)
    tenant, product_id = images_db.tenant("acme"), _null_product(images_db)
    image_id = image_row(images_db.conn, tenant, product_id, slot="main")
    key = image_db_row(images_db.conn, image_id)["storage_key"]
    store.objects[key] = (picture(1200, 1200), "image/jpeg", datetime.now(UTC))
    job = await enqueue_image_job(tenant, image_id)
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    reports = image_db_row(images_db.conn, image_id)["image_report_json"]
    assert set(reports) == {"amazon", "flipkart"}


async def test_size_chart_read_runs_without_category(
    images_db: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FakeStorage()
    use_read_storage(monkeypatch, store)
    tenant, product_id = images_db.tenant("acme"), _null_product(images_db)
    image, _digest = chart_image(images_db.conn, store, tenant, product_id)
    job = await enqueue_read(tenant, image)
    await run_image_job(job)
    assert job_row(images_db.conn, job)["status"] == "succeeded"
    state = read_state(images_db.conn, image)
    assert state is not None and state["status"] == "read"
