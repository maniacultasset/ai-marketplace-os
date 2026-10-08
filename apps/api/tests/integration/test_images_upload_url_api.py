"""S17b T8 (AC5, AC6; PLAN D1, D2, D7, D9): `POST /api/v1/images/upload-url` as app_rw: the
case matrix, the D1 key, the create-only signed PUT against the compose MinIO, no DB write and
no signed URL in any log line."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.adapters.storage import get_storage, images_bucket
from app.services.image_keys import parse_image_key

from .conftest import LOCAL_HOSTS
from .images_support import UPLOAD_URL, images_db, product, style_and_child
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]


def body(product_id: Any, **over: Any) -> dict[str, Any]:
    base = {"product_id": str(product_id), "slot": "main", "content_type": "image/jpeg", "bytes": 5}
    return {**base, **over}


async def mint(
    jw: JobWorld, api: httpx.AsyncClient, email: str = "operator@acme.test", **over: Any
) -> httpx.Response:
    p = over.pop("product_id", None) or product(
        jw.conn, jw.tenant(email.split("@")[1].split(".")[0])
    )
    return await api.post(UPLOAD_URL, headers=as_member(jw, email), json=body(p, **over))


def put(out: dict[str, Any], data: bytes, **headers: str) -> httpx.Response:
    sent = {**out["headers"], **headers}
    return httpx.put(out["upload_url"], content=data, headers={k: v for k, v in sent.items() if v})


@pytest.mark.endpoint("images_upload_url", case="success")
@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_upload_url_success(images_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    response = await mint(images_db, api, f"{role}@acme.test")
    assert response.status_code == 200, response.text
    out = response.json()
    assert set(out) == {"upload_url", "storage_key", "expires_at", "headers"}
    assert out["headers"] == {"Content-Type": "image/jpeg", "If-None-Match": "*"}


async def test_upload_url_key_matches_d1(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    keys = [(await mint(images_db, api, product_id=p)).json()["storage_key"] for _ in range(2)]
    parsed = [parse_image_key(k) for k in keys]
    assert all(k is not None and k.tenant_id == acme and k.product_id == p for k in parsed)
    assert parsed[0].image_id != parsed[1].image_id  # type: ignore[union-attr]


async def test_upload_url_put_round_trip(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    out = (await mint(images_db, api)).json()
    assert put(out, b"12345").status_code == 200
    head = await get_storage().head(images_bucket(), out["storage_key"])
    assert head is not None and head.size == 5


async def test_upload_url_other_length_refused(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    out = (await mint(images_db, api)).json()
    assert put(out, b"123456").status_code == 403
    assert await get_storage().head(images_bucket(), out["storage_key"]) is None


async def test_upload_url_other_type_refused(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    out = (await mint(images_db, api)).json()
    assert put(out, b"12345", **{"Content-Type": "image/png"}).status_code == 403
    assert await get_storage().head(images_bucket(), out["storage_key"]) is None


async def test_upload_url_headers_include_if_none_match(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    out = (await mint(images_db, api)).json()
    signed = parse_qs(urlsplit(out["upload_url"]).query)["X-Amz-SignedHeaders"][0].split(";")
    assert {"content-type", "content-length", "if-none-match", "host"} <= set(signed)


async def test_upload_url_second_put_412(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    out = (await mint(images_db, api)).json()
    first = put(out, b"12345")
    assert first.status_code == 200
    assert put(out, b"54321").status_code == 412
    assert await get_storage().get(images_bucket(), out["storage_key"]) == b"12345"


async def test_upload_url_no_db_write(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    def counts() -> tuple[int, int, int]:
        return tuple(  # type: ignore[return-value]
            count(images_db.conn, f"SELECT count(*) FROM {t}")  # noqa: S608 - fixed names
            for t in ("images", "job_runs", "audit_log")
        )

    before = counts()
    assert (await mint(images_db, api)).status_code == 200
    assert counts() == before


async def test_upload_url_webp_ok(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await mint(images_db, api, content_type="image/webp")
    assert response.status_code == 200
    assert response.json()["headers"]["Content-Type"] == "image/webp"


@pytest.mark.endpoint("images_upload_url", case="validation")
@pytest.mark.parametrize(
    "over",
    [
        {"slot": "hero"},
        {"content_type": "image/gif"},
        {"content_type": "image/tiff"},
        {"bytes": 0},
        {"bytes": 10485761},
        {"bytes": "NaN"},
        {"bytes": "1e3"},
        {"tenant_id": str(uuid.uuid4())},
        {"product_id": "not-a-uuid"},
    ],
    ids=["slot", "gif", "tiff", "zero", "over_cap", "nan", "exp", "extra_tenant", "bad_product"],
)
async def test_upload_url_validation(
    images_db: JobWorld, api: httpx.AsyncClient, over: dict[str, Any]
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    sent = {**body(p), **over}
    response = await api.post(
        UPLOAD_URL, headers=as_member(images_db, "operator@acme.test"), json=sent
    )
    assert response.status_code == 422, response.text


async def test_upload_url_exact_cap_accepted(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await mint(images_db, api, bytes=10485760)).status_code == 200


async def test_upload_url_size_chart_on_child_422(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    acme = images_db.tenant("acme")
    style, child = style_and_child(images_db.conn, acme)
    response = await mint(images_db, api, product_id=child, slot="size_chart")
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "slot"]
    assert (await mint(images_db, api, product_id=style, slot="size_chart")).status_code == 200


@pytest.mark.endpoint("images_upload_url", case="missing_auth")
async def test_upload_url_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.post(UPLOAD_URL, json=body(uuid.uuid4()))).status_code == 401


@pytest.mark.endpoint("images_upload_url", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_upload_url", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_upload_url", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_upload_url_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.post(
        UPLOAD_URL, headers=bad_token(images_db, variant), json=body(uuid.uuid4())
    )
    assert response.status_code == 401


@pytest.mark.endpoint("images_upload_url", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_upload_url_wrong_role(
    images_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    assert (await mint(images_db, api, f"{role}@acme.test")).status_code == 403


async def test_upload_url_deactivated_member_401(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    images_db.conn.execute(
        "UPDATE tenant_memberships SET is_active = false WHERE user_id = %s",
        [images_db.world.user("operator@acme.test")],
    )
    assert (await mint(images_db, api)).status_code == 401


@pytest.mark.endpoint("images_upload_url", case="wrong_tenant")
async def test_upload_url_wrong_tenant(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    theirs = product(images_db.conn, images_db.tenant("acme"))
    response = await mint(images_db, api, "operator@bharat.test", product_id=theirs)
    missing = await mint(images_db, api, "operator@bharat.test", product_id=uuid.uuid4())
    assert response.status_code == missing.status_code == 404
    assert response.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.endpoint("images_upload_url", case="not_found")
async def test_upload_url_not_found(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    acme = images_db.tenant("acme")
    gone = product(images_db.conn, acme, deleted_at=datetime.now(UTC))
    for p in (uuid.uuid4(), gone):
        response = await mint(images_db, api, product_id=p)
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_signed_url_not_logged(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("", "boto3", "botocore", "urllib3"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.DEBUG
    out = (await mint(images_db, api)).json()
    signature = parse_qs(urlsplit(out["upload_url"]).query)["X-Amz-Signature"][0]
    # the app's own log lines only (the test's httpx client below logs its request URL)
    logged = caplog.text + "".join(capfd.readouterr())
    assert signature not in logged
    assert "X-Amz-Signature=" not in logged or "X-Amz-Signature=[REDACTED]" in logged
    assert put(out, b"12345").status_code == 200
