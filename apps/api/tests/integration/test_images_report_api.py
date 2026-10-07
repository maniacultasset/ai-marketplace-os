"""S17b T12 (AC5, AC9; CP4): `GET /api/v1/images/{image_id}/report` and the S14b
`validate_images` port returning the stored report the job wrote."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mkos_schemas.enums import Marketplace
from mkos_schemas.images import ImageReport

from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    enqueue_image_job,
    image_db_row,
    image_row,
    images_db,
    picture,
    product,
    report_url,
    run_image_job,
    use_storage,
)
from .jobs_support import JobWorld, as_member, bad_token
from .products_support import without_correlation

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


async def checked_image(
    jw: JobWorld, store: FakeStorage, slug: str = "acme", data: bytes | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = jw.tenant(slug)
    p = product(jw.conn, tenant)
    image_id = image_row(jw.conn, tenant, p, slot="main")
    store.objects[image_db_row(jw.conn, image_id)["storage_key"]] = (
        data or picture(800, 800),
        "image/jpeg",
        datetime.now(UTC),
    )
    await run_image_job(await enqueue_image_job(tenant, image_id))
    return image_id, p


def me(jw: JobWorld, role: str = "reviewer", slug: str = "acme") -> dict[str, str]:
    return as_member(jw, f"{role}@{slug}.test")


@pytest.mark.endpoint("images_report", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_images_report_each_role_ok(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, role: str
) -> None:
    image_id, _ = await checked_image(images_db, store)
    response = await api.get(report_url(image_id), headers=me(images_db, role))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["image_id"] == str(image_id) and body["compliance_status"] == "failed"
    assert set(body["reports"]) == {"amazon", "flipkart"}
    for market, report in body["reports"].items():
        assert "pass" in report and "pass_" not in report
        assert ImageReport.model_validate(report).marketplace.value == market
    stored = image_db_row(images_db.conn, image_id)["image_report_json"]
    assert body["reports"] == stored


async def test_images_report_pending_empty(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    image_id = image_row(images_db.conn, acme, product(images_db.conn, acme))
    body = (await api.get(report_url(image_id), headers=me(images_db))).json()
    assert body["compliance_status"] == "pending"
    assert body["reports"] in ({}, {"amazon": None, "flipkart": None})


@pytest.mark.endpoint("images_report", case="validation")
async def test_images_report_bad_uuid(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(report_url("not-a-uuid"), headers=me(images_db))).status_code == 422


@pytest.mark.endpoint("images_report", case="missing_auth")
async def test_images_report_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(report_url(uuid.uuid4()))).status_code == 401


@pytest.mark.endpoint("images_report", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_report", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_report", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_images_report_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(report_url(uuid.uuid4()), headers=bad_token(images_db, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("images_report", case="wrong_tenant")
async def test_images_report_wrong_tenant(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    theirs, _ = await checked_image(images_db, store, "bharat")
    response = await api.get(report_url(theirs), headers=me(images_db))
    missing = await api.get(report_url(uuid.uuid4()), headers=me(images_db))
    assert response.status_code == missing.status_code == 404
    assert without_correlation(response.json()) == without_correlation(missing.json())


@pytest.mark.endpoint("images_report", case="not_found")
async def test_images_report_deleted_404_same_bytes_as_random(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, _ = await checked_image(images_db, store)
    images_db.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [image_id])
    gone = await api.get(report_url(image_id), headers=me(images_db))
    missing = await api.get(report_url(uuid.uuid4()), headers=me(images_db))
    assert gone.status_code == missing.status_code == 404
    assert gone.json()["error"]["type"] == "NOT_FOUND"
    assert without_correlation(gone.json()) == without_correlation(missing.json())


async def test_validate_images_returns_stored_report(
    images_db: JobWorld, store: FakeStorage
) -> None:
    """S14b's builder preloads what the job stored: the checked image's Amazon report for an
    Amazon listing; nothing for a Flipkart listing built for Amazon; an unchecked image omitted."""
    from app.repositories.uow import tenant_uow
    from app.services.pipeline_deps import build_pipeline_deps
    from app.services.rules_loader import load_rules
    from mkos_schemas.listing import CanonicalListing

    from .test_pipeline_ports_a import settings

    acme = images_db.tenant("acme")
    image_id, p = await checked_image(images_db, store)
    unchecked = image_row(images_db.conn, acme, p)
    async with tenant_uow(acme) as uow:
        rules = await load_rules(uow, tenant_id=acme, marketplace="amazon", category="kurtas")

    def listing(marketplace: str) -> Any:
        return CanonicalListing.model_validate(
            {
                "listing_id": str(uuid.uuid4()),
                "product_id": str(p),
                "marketplace": marketplace,
                "category": "kurtas",
                "content": {"title": "A kurta", "bullets": ["one"], "description": "d"},
                "images": [
                    {"image_id": str(image_id), "slot": "main"},
                    {"image_id": str(unchecked), "slot": "secondary"},
                ],
            }
        )

    deps = await build_pipeline_deps(
        settings=settings(), tenant_id=acme, job_id=uuid.uuid4(), user_id=None, product_id=p,
        marketplace=Marketplace.AMAZON, rules=rules,
    )  # fmt: skip
    found = await deps.validate_images(listing("amazon"))
    stored = image_db_row(images_db.conn, image_id)["image_report_json"]["amazon"]
    assert [r.model_dump(mode="json", by_alias=True) for r in found] == [stored]
    assert await deps.validate_images(listing("flipkart")) == []
