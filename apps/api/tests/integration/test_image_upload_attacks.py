"""S17b tester round 1 (AC2, AC5, AC7): attacks on `POST /api/v1/images/upload` that the
implementer's suites leave open: concurrent replays under one Idempotency-Key on both branches,
another tenant's colourway, a product deleted after its URL was minted, and hostile or oversized
`storage_key` values. Real API, app_rw under RLS, fake object store."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from app.services.image_keys import image_object_key

from . import factories as f
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
from .jobs_support import JobWorld, count
from .products_support import without_correlation

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
JPEG = picture(64, 64)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


def _rows(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM images")


def _jobs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM job_runs WHERE kind = 'image_compliance'")


async def test_concurrent_multipart_same_idem_key_one_row(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    """Two in-flight multipart uploads with one Idempotency-Key and one body: both 202 with the
    same body, one row, one job; the loser's object is at most an orphan (AC7)."""
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    a, b = await asyncio.gather(
        upload_file(api, hdrs, p, JPEG, slot="secondary"),
        upload_file(api, hdrs, p, JPEG, slot="secondary"),
    )
    assert a.status_code == b.status_code == 202, (a.text, b.text)
    assert a.json() == b.json()
    assert (_rows(images_db), _jobs(images_db)) == (1, 1)
    registered = images_db.conn.execute("SELECT storage_key FROM images").fetchone()
    assert registered is not None and registered[0] in store.objects


async def test_concurrent_key_same_idem_key_one_row(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = image_object_key(acme, p, uuid.uuid4())
    store.objects[key] = (JPEG, "image/jpeg", datetime.now(UTC))
    hdrs = headers(images_db)
    a, b = await asyncio.gather(register_key(api, hdrs, p, key), register_key(api, hdrs, p, key))
    assert a.status_code == b.status_code == 202, (a.text, b.text)
    assert a.json() == b.json()
    assert (_rows(images_db), _jobs(images_db)) == (1, 1)


async def test_concurrent_multipart_same_idem_key_other_body(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    """One key, two bodies in flight: one 202 and one 409, never two rows."""
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    a, b = await asyncio.gather(
        upload_file(api, hdrs, p, JPEG, slot="secondary"),
        upload_file(api, hdrs, p, picture(65, 64), slot="secondary"),
    )
    assert sorted([a.status_code, b.status_code]) == [202, 409], (a.text, b.text)
    assert (_rows(images_db), _jobs(images_db)) == (1, 1)


async def test_colourway_of_other_tenant_same_as_missing(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    """Another tenant's colourway id gives exactly the bytes of a random id (no existence leak)."""
    acme, bharat = images_db.tenant("acme"), images_db.tenant("bharat")
    style = product(images_db.conn, acme, "style", variation_theme="color")
    b_style = product(images_db.conn, bharat, "style", variation_theme="color")
    b_cw = f.colourway(images_db.conn, bharat, b_style)
    foreign = await upload_file(api, headers(images_db), style, JPEG, colourway_id=b_cw)
    missing = await upload_file(api, headers(images_db), style, JPEG, colourway_id=uuid.uuid4())
    assert foreign.status_code == missing.status_code == 422, foreign.text
    assert without_correlation(foreign.json()) == without_correlation(missing.json())
    assert _rows(images_db) == 0
    assert store.objects == {}


async def test_product_deleted_after_mint_404_no_head(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = image_object_key(acme, p, uuid.uuid4())
    store.objects[key] = (JPEG, "image/jpeg", datetime.now(UTC))
    images_db.conn.execute("UPDATE products SET deleted_at = now() WHERE id = %s", [p])
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 404, response.text
    assert "head" not in store.calls
    assert _rows(images_db) == 0


@pytest.mark.parametrize(
    "case",
    [
        "uppercase_uuids",
        "unicode",
        "nul",
        "newline_suffix",
        "dotdot_segment",
        "oversized",
        "extra_segment",
    ],
)
async def test_hostile_storage_key_422_no_store_call(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, case: str
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    good = image_object_key(acme, p, uuid.uuid4())
    key = {
        "uppercase_uuids": good.upper().replace("IMAGES", "images"),
        "unicode": good[:-1] + chr(0x430),  # a Cyrillic a
        "nul": good + "\x00",
        "newline_suffix": good + "\n",
        "dotdot_segment": f"{acme}/{p}/images/../{uuid.uuid4()}",
        "oversized": good + "a" * 5000,
        "extra_segment": good + "/x",
    }[case]
    store.objects[key] = (JPEG, "image/jpeg", datetime.now(UTC))
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 422, (case, response.status_code, response.text)
    assert store.calls == []
    assert _rows(images_db) == 0
