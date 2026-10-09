"""S17b T16 (DW1, DW2; AC1, AC2, AC13): the image pipeline end to end over HTTP against the
compose MinIO and the real child process: upload-url → signed PUT → register → the job → the
report; the multipart path; another tenant's ids are 404 everywhere; no model call."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.storage import ObjectStorage
from app.services.image_keys import image_object_key

from .conftest import LOCAL_HOSTS
from .images_support import (
    UPLOAD_URL,
    headers,
    image_db_row,
    images_db,
    picture,
    product,
    register_key,
    report_url,
    run_image_job,
    upload_file,
)
from .jobs_support import JobWorld, as_member, count, job_row
from .products_support import PRODUCTS, without_correlation

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
WHITE_800 = picture(800, 800)


async def _check_storage() -> None:
    from app.adapters.storage import get_storage, images_bucket
    from app.core.config import get_settings

    try:
        get_storage()._client.head_bucket(Bucket=images_bucket())
    except Exception:
        pytest.fail(f"MinIO not reachable on {get_settings().s3_endpoint_url}")


async def signed_upload(
    jw: JobWorld, api: httpx.AsyncClient, p: uuid.UUID, data: bytes
) -> dict[str, Any]:
    minted = await api.post(
        UPLOAD_URL,
        headers=as_member(jw, "operator@acme.test"),
        json={
            "product_id": str(p),
            "slot": "main",
            "content_type": "image/jpeg",
            "bytes": len(data),
        },
    )
    assert minted.status_code == 200, minted.text
    out = minted.json()
    async with httpx.AsyncClient() as client:
        put = await client.put(out["upload_url"], content=data, headers=out["headers"])
    assert put.status_code == 200, put.text
    registered = await register_key(api, headers(jw), p, out["storage_key"])
    assert registered.status_code == 202, registered.text
    return registered.json()  # type: ignore[no-any-return]


async def test_image_upload_flow(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _check_storage()
    p = product(images_db.conn, images_db.tenant("acme"))
    accepted = await signed_upload(images_db, api, p, WHITE_800)
    job_id = uuid.UUID(accepted["job_id"])
    await run_image_job(job_id)
    assert count(images_db.conn, "SELECT count(*) FROM images") == 1
    assert job_row(images_db.conn, job_id)["status"] == "succeeded"
    row = image_db_row(images_db.conn, accepted["image_id"])
    assert row["compliance_status"] == "failed"
    amazon, flipkart = row["image_report_json"]["amazon"], row["image_report_json"]["flipkart"]
    assert amazon["pass"] is False and [i["code"] for i in amazon["issues"]] == ["RESOLUTION_LOW"]
    assert flipkart["pass"] is True and [i["code"] for i in flipkart["issues"]] == [
        "RESOLUTION_LOW"
    ]
    report = await api.get(
        report_url(accepted["image_id"]), headers=as_member(images_db, "reviewer@acme.test")
    )
    assert report.status_code == 200
    assert report.json()["reports"] == row["image_report_json"]
    assert report.json()["compliance_status"] == "failed"


async def test_image_upload_multipart_flow(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _check_storage()
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, picture(1000, 1000, fmt="PNG"))
    assert response.status_code == 202, response.text
    accepted = response.json()
    await run_image_job(uuid.UUID(accepted["job_id"]))
    body = (
        await api.get(
            report_url(accepted["image_id"]), headers=as_member(images_db, "operator@acme.test")
        )
    ).json()
    assert body["compliance_status"] == "passed"
    assert {m: r["pass"] for m, r in body["reports"].items()} == {"amazon": True, "flipkart": True}
    listed = (
        await api.get(f"{PRODUCTS}/{p}", headers=as_member(images_db, "reviewer@acme.test"))
    ).json()
    assert listed["images"] == [image_db_row(images_db.conn, accepted["image_id"])["storage_key"]]


async def test_image_cross_tenant(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DW2: bharat never reaches acme's image, product, upload URL or key; `head` and `get` are
    never called for acme's key."""
    await _check_storage()
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    accepted = await signed_upload(images_db, api, p, WHITE_800)
    await run_image_job(uuid.UUID(accepted["job_id"]))
    bharat = as_member(images_db, "operator@bharat.test")

    report = await api.get(report_url(accepted["image_id"]), headers=bharat)
    random = await api.get(report_url(uuid.uuid4()), headers=bharat)
    assert report.status_code == random.status_code == 404
    assert without_correlation(report.json()) == without_correlation(random.json())
    assert (await api.get(f"{PRODUCTS}/{p}", headers=bharat)).status_code == 404
    minted = await api.post(
        UPLOAD_URL,
        headers=bharat,
        json={"product_id": str(p), "slot": "main", "content_type": "image/jpeg", "bytes": 5},
    )
    assert minted.status_code == 404

    touched: list[str] = []
    for name in ("head", "get"):
        real = getattr(ObjectStorage, name)

        async def spy(
            self: ObjectStorage, *a: Any, _real: Any = real, _name: str = name, **k: Any
        ) -> Any:
            touched.append(_name)
            return await _real(self, *a, **k)

        monkeypatch.setattr(ObjectStorage, name, spy)
    mine = product(images_db.conn, images_db.tenant("bharat"))
    acme_key = image_object_key(acme, mine, uuid.uuid4())
    by_key = await register_key(api, headers(images_db, "operator@bharat.test"), mine, acme_key)
    assert by_key.status_code == 404
    assert touched == []
    assert (
        count(
            images_db.conn,
            "SELECT count(*) FROM images WHERE tenant_id = %s",
            [images_db.tenant("bharat")],
        )
        == 0
    )


async def test_image_flow_writes_no_agent_runs(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    await _check_storage()
    p = product(images_db.conn, images_db.tenant("acme"))
    accepted = await signed_upload(images_db, api, p, WHITE_800)
    await run_image_job(uuid.UUID(accepted["job_id"]))
    assert count(images_db.conn, "SELECT count(*) FROM agent_runs") == 0
