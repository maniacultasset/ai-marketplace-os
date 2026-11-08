"""SI7 T15 (AC11, AC14, AC15): a candidate never reaches the `images` readers, and the product
read carries the provenance and the `fixes` an image offers."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.repositories.uow import tenant_uow
from app.services.generation_images import order_for_listing, pick_context
from app.services.image_keys import candidate_key, parse_derived_key, parse_image_key

from .candidates_support import candidate_row, enqueue_candidate, source_main, use_candidate_storage
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, image_row, images_db, product, run_image_job
from .jobs_support import JobWorld, as_member

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
FAILED_BACKGROUND = {
    "checks": [
        {"check": "resolution", "method": "deterministic", "passed": True},
        {"check": "background", "method": "deterministic", "passed": False},
    ]
}
FAILED_ASPECT = {"checks": [{"check": "aspect_ratio", "method": "deterministic", "passed": False}]}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_candidate_storage(monkeypatch, fake)
    return fake


async def test_a_ready_candidate_reaches_no_image_reader(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p, src, digest = source_main(images_db.conn, store, tenant)
    candidate, job = await enqueue_candidate(tenant, p, src, digest)
    await run_image_job(job)
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"
    async with tenant_uow(tenant) as uow:
        rows = await uow.images.listing_set(p)
        assert [r.image_id for r in rows] == [src]
        assert [r.image_id for r in pick_context(rows)] == [src]
        assert [ref.image_id for ref in order_for_listing(rows)] == [src]
        assert await uow.images.count_live(p) == 1
        assert await uow.images.count_live_image_jobs() == 0
        keys = await uow.images.live_keys_for_products([p])
    assert all("candidates" not in k for k in keys[p])
    response = await api.get(
        f"/api/v1/products/{p}", headers=as_member(images_db, "operator@acme.test")
    )
    assert [d["image_id"] for d in response.json()["image_details"]] == [str(src)]
    key = candidate_key(tenant, p, candidate)
    assert parse_image_key(key) is None and parse_derived_key(key) is None


def _image(jw: JobWorld, tenant: uuid.UUID, p: uuid.UUID, **cols: Any) -> uuid.UUID:
    return image_row(jw.conn, tenant, p, **cols)


async def _details(api: httpx.AsyncClient, jw: JobWorld, p: uuid.UUID) -> dict[str, Any]:
    response = await api.get(f"/api/v1/products/{p}", headers=as_member(jw, "operator@acme.test"))
    assert response.status_code == 200, response.text
    return {d["image_id"]: d for d in response.json()["image_details"]}


async def test_product_read_offers_fixes_and_shows_provenance(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    tenant = images_db.tenant("acme")
    grey = product(images_db.conn, tenant)
    main = _image(
        images_db,
        tenant,
        grey,
        slot="main",
        image_report_json={"amazon": FAILED_BACKGROUND},
        metadata={"border": {"uniform": True, "near_white": False, "colour": [200, 200, 200]}},
    )
    made = _image(
        images_db,
        tenant,
        grey,
        slot="secondary",
        origin="rendered",
        parent_image_id=main,
        candidate_id=uuid.uuid4(),
    )
    off = await _details(api, images_db, grey)
    assert off[str(main)]["fixes"] == []  # the switch is off: no white_background
    assert off[str(main)]["origin"] == "seller" and off[str(main)]["parent_image_id"] is None
    assert off[str(made)]["origin"] == "rendered" and off[str(made)]["parent_image_id"] == str(main)
    monkeypatch.setenv("IMAGE_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        on = await _details(api, images_db, grey)
    finally:
        get_settings.cache_clear()
    assert on[str(main)]["fixes"] == ["white_background"]
    assert on[str(made)]["fixes"] == []
    framing = product(images_db.conn, tenant)
    aspect = _image(
        images_db, tenant, framing, slot="main", image_report_json={"amazon": FAILED_ASPECT}
    )
    assert (await _details(api, images_db, framing))[str(aspect)]["fixes"] == ["fix"]


async def test_pre_si7_row_falls_back(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    main = _image(
        images_db, tenant, p, slot="main", image_report_json={"amazon": FAILED_BACKGROUND}
    )
    monkeypatch.setenv("IMAGE_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        details = await _details(api, images_db, p)
    finally:
        get_settings.cache_clear()
    assert details[str(main)]["fixes"] == ["white_background"]
