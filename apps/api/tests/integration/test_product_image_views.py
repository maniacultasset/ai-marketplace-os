"""Saved photo views: a product read signs each checked image's server-made copy (`view_url`), and
the list signs each product's main photo (`thumbnail_url`), so a photo shows after a reload."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import image_row, images_db, product, style_and_child
from .jobs_support import JobWorld, as_member
from .products_support import PRODUCTS, statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]

HASH = "a" * 64
VISION = {"vision": {"w": 768, "h": 1024}}


def me(jw: JobWorld) -> dict[str, str]:
    return as_member(jw, "reviewer@acme.test")


def checked(jw: JobWorld, tenant: uuid.UUID, p: uuid.UUID, **cols: Any) -> uuid.UUID:
    """An image the compliance job has derived (clean JPEG and the vision copy)."""
    cols.setdefault("clean_content_hash", HASH)
    cols.setdefault("clean_mime_type", "image/jpeg")
    cols.setdefault("metadata", VISION)
    return image_row(jw.conn, tenant, p, **cols)


def path_of(url: str) -> str:
    return urlsplit(url).path


async def read(jw: JobWorld, api: httpx.AsyncClient, p: uuid.UUID) -> dict[str, Any]:
    response = await api.get(f"{PRODUCTS}/{p}", headers=me(jw))
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_detail_signs_the_vision_copy(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    main = checked(images_db, acme, p, slot="main")
    before = datetime.now(UTC)
    body = await read(images_db, api, p)
    (detail,) = body["image_details"]
    assert detail["image_id"] == str(main)
    url = detail["view_url"]
    assert path_of(url).endswith(f"/{acme}/{p}/derived/{main}/vision.jpg")
    assert "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url
    expires = datetime.fromisoformat(detail["view_expires_at"])
    assert before + timedelta(seconds=560) <= expires <= before + timedelta(seconds=600)
    assert body["thumbnail_url"] is not None
    assert path_of(body["thumbnail_url"]) == path_of(url)
    assert body["thumbnail_expires_at"] is not None


async def test_detail_falls_back_to_the_clean_copy(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    sec = checked(images_db, acme, p, slot="secondary", clean_mime_type="image/png", metadata={})
    (detail,) = (await read(images_db, api, p))["image_details"]
    assert path_of(detail["view_url"]).endswith(f"/derived/{sec}/clean.png")


async def test_unchecked_photo_has_no_view(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    """Before the derive, there is nothing safe to show: the original upload is never signed."""
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    image_row(images_db.conn, acme, p, slot="main")
    body = await read(images_db, api, p)
    (detail,) = body["image_details"]
    assert detail["view_url"] is None and detail["view_expires_at"] is None
    assert body["thumbnail_url"] is None and body["thumbnail_expires_at"] is None


async def test_a_failed_photo_still_shows_to_its_seller(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """The seller must see a photo that failed its checks, from the private bucket only."""
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    checked(images_db, acme, p, slot="main", compliance_status="failed")
    (detail,) = (await read(images_db, api, p))["image_details"]
    assert detail["compliance_status"] == "failed"
    assert "/derived/" in path_of(detail["view_url"])


async def test_list_signs_each_main_photo(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    with_photo = product(images_db.conn, acme)
    main = checked(images_db, acme, with_photo, slot="main")
    checked(images_db, acme, with_photo, slot="secondary")
    without = product(images_db.conn, acme)
    checked(images_db, acme, without, slot="secondary")
    with statements() as seen:
        response = await api.get(PRODUCTS, headers=me(images_db), params={"limit": 10})
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    url = rows[str(with_photo)]["thumbnail_url"]
    assert path_of(url).endswith(f"/derived/{main}/vision.jpg")
    assert rows[str(with_photo)]["thumbnail_expires_at"] is not None
    assert rows[str(without)]["thumbnail_url"] is None  # only a main photo is a thumbnail
    assert len([s for s in seen if "FROM images" in s]) == 2  # constant per page


async def test_variant_shows_its_style_photo(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    style, child = style_and_child(images_db.conn, acme)
    main = checked(images_db, acme, style, slot="main")
    response = await api.get(PRODUCTS, headers=me(images_db), params={"limit": 10})
    rows = {row["id"]: row for row in response.json()["data"]}
    assert path_of(rows[str(child)]["thumbnail_url"]).endswith(f"/derived/{main}/vision.jpg")
    detail = await read(images_db, api, child)
    assert path_of(detail["thumbnail_url"]).endswith(f"/derived/{main}/vision.jpg")


async def test_views_stay_in_the_callers_tenant(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    acme = images_db.tenant("acme")
    other = f.tenant(images_db.conn)
    theirs = product(images_db.conn, other)
    checked(images_db, other, theirs, slot="main")
    mine = product(images_db.conn, acme)
    checked(images_db, acme, mine, slot="main")
    response = await api.get(f"{PRODUCTS}/{theirs}", headers=me(images_db))
    assert response.status_code == 404
    listed = (await api.get(PRODUCTS, headers=me(images_db), params={"limit": 50})).json()["data"]
    assert str(theirs) not in {row["id"] for row in listed}
    for row in listed:
        if row["thumbnail_url"] is not None:
            assert path_of(row["thumbnail_url"]).split("/")[2] == str(acme)
