"""S17b T13 (AC11; PLAN D11; CP7): `ProductOut.images` lists the product's live image keys in
slot order, one images query per page; `images` on create stays 404."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .images_support import image_db_row, image_row, images_db, product
from .jobs_support import JobWorld, as_member
from .products_support import PRODUCTS, site_body, statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]


def me(jw: JobWorld) -> dict[str, str]:
    return as_member(jw, "reviewer@acme.test")


def keys_of(jw: JobWorld, *ids: object) -> list[str]:
    return [image_db_row(jw.conn, i)["storage_key"] for i in ids]


async def test_product_images_listed(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    pack = image_row(images_db.conn, acme, p, slot="packaging_label")
    sec1 = image_row(images_db.conn, acme, p, slot="secondary", position=1)
    life = image_row(images_db.conn, acme, p, slot="lifestyle")
    sec0 = image_row(images_db.conn, acme, p, slot="secondary", position=0)
    main = image_row(images_db.conn, acme, p, slot="main")
    body = (await api.get(f"{PRODUCTS}/{p}", headers=me(images_db))).json()
    assert body["images"] == keys_of(images_db, main, sec0, sec1, life, pack)


async def test_product_images_skip_deleted(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    image_row(images_db.conn, acme, p, slot="main", deleted_at=datetime.now(UTC))
    live = image_row(images_db.conn, acme, p, slot="main")
    body = (await api.get(f"{PRODUCTS}/{p}", headers=me(images_db))).json()
    assert body["images"] == keys_of(images_db, live)


async def test_product_list_images_one_query(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    products = [product(images_db.conn, acme) for _ in range(5)]
    for p in products:
        image_row(images_db.conn, acme, p, slot="main")
    with statements() as seen:
        response = await api.get(PRODUCTS, headers=me(images_db), params={"limit": 10})
    assert response.status_code == 200
    by_id = {row["id"]: row["images"] for row in response.json()["data"]}
    assert all(len(by_id[str(p)]) == 1 for p in products)
    assert len([s for s in seen if "FROM images" in s]) == 1


async def test_product_create_images_still_404(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = image_db_row(images_db.conn, image_row(images_db.conn, acme, p))["storage_key"]
    response = await api.post(
        PRODUCTS,
        headers=as_member(images_db, "operator@acme.test"),
        json=site_body("S17B-IMG-1", images=[key]),
    )
    assert response.status_code == 404
