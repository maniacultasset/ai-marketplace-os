"""S17b T11 (AC5): the `POST /api/v1/images/upload` route: the case matrix, the body cap, the
exactly-one rule, the fault order and the job it starts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from app.services.image_keys import image_object_key

from .conftest import LOCAL_HOSTS
from .images_support import (
    UPLOAD,
    FakeStorage,
    headers,
    images_db,
    picture,
    product,
    register_key,
    upload_file,
    use_storage,
)
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
JPEG = picture(64, 64)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


@pytest.mark.endpoint("images_upload", case="success")
@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_upload_success(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, role: str
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db, f"{role}@acme.test"), p, JPEG)
    assert response.status_code == 202, response.text
    assert set(response.json()) == {"image_id", "job_id", "status", "poll_url"}


async def test_upload_then_job_get(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    body = (await upload_file(api, headers(images_db), p, JPEG)).json()
    job = await api.get(body["poll_url"], headers=as_member(images_db, "reviewer@acme.test"))
    assert job.status_code == 200
    assert (job.json()["job_id"], job.json()["status"]) == (body["job_id"], "queued")


@pytest.mark.endpoint("images_upload", case="validation")
@pytest.mark.parametrize(
    "case",
    ["no_idem_key", "file_and_key", "neither", "bad_slot", "bad_key_shape", "bad_product_id"],
)
async def test_upload_validation(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, case: str
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = image_object_key(acme, p, uuid.uuid4())
    hdrs = headers(images_db)
    data: dict[str, str] = {"product_id": str(p), "slot": "main"}
    files = {"file": ("x.jpg", JPEG, "image/jpeg")}
    if case == "no_idem_key":
        hdrs.pop("Idempotency-Key")
        response = await api.post(UPLOAD, headers=hdrs, data=data, files=files)
    elif case == "file_and_key":
        response = await api.post(
            UPLOAD, headers=hdrs, data={**data, "storage_key": key}, files=files
        )
    elif case == "neither":
        response = await api.post(UPLOAD, headers=hdrs, data=data)
    elif case == "bad_slot":
        response = await api.post(UPLOAD, headers=hdrs, data={**data, "slot": "hero"}, files=files)
    elif case == "bad_key_shape":
        response = await api.post(UPLOAD, headers=hdrs, data={**data, "storage_key": "../x"})
    else:
        response = await api.post(
            UPLOAD, headers=hdrs, data={**data, "product_id": "nope"}, files=files
        )
    assert response.status_code == 422, (case, response.text)
    assert count(images_db.conn, "SELECT count(*) FROM images") == 0
    assert store.objects == {}


async def test_upload_body_over_cap_413(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, b"x" * (10_485_760 + 10))
    assert response.status_code == 413
    assert store.objects == {}


async def test_upload_exact_10mib_file_413(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    """The effective multipart limit is the 10 MiB request cap: the parts' framing pushes a file
    of exactly 10 MiB over it (FE notice)."""
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, b"x" * 10_485_760)
    assert response.status_code == 413


async def test_upload_body_cap_before_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(UPLOAD, files={"file": ("x", b"x" * (10_485_760 + 10), "image/jpeg")})
    assert response.status_code == 413


@pytest.mark.endpoint("images_upload", case="missing_auth")
async def test_upload_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(UPLOAD, data={"product_id": str(uuid.uuid4()), "slot": "main"})
    assert response.status_code == 401


@pytest.mark.endpoint("images_upload", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_upload", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_upload", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_upload_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    response = await upload_file(api, hdrs, uuid.uuid4(), JPEG)
    assert response.status_code == 401


@pytest.mark.endpoint("images_upload", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_upload_wrong_role(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, role: str
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db, f"{role}@acme.test"), p, JPEG)
    assert response.status_code == 403
    assert store.objects == {}


async def test_upload_fault_order(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    bad = {"slot": "hero"}
    assert (await api.post(UPLOAD, data=bad)).status_code == 401
    hdrs = headers(images_db, "reviewer@acme.test")
    assert (await api.post(UPLOAD, headers=hdrs, data=bad)).status_code == 403


@pytest.mark.endpoint("images_upload", case="wrong_tenant")
async def test_upload_wrong_tenant(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    theirs = product(images_db.conn, acme)
    bharat_hdrs = headers(images_db, "operator@bharat.test")
    by_product = await upload_file(api, bharat_hdrs, theirs, JPEG)
    random = await upload_file(api, headers(images_db, "operator@bharat.test"), uuid.uuid4(), JPEG)
    assert by_product.status_code == random.status_code == 404
    assert by_product.json()["error"]["message"] == random.json()["error"]["message"]
    key = image_object_key(acme, theirs, uuid.uuid4())
    store.objects[key] = (JPEG, "image/jpeg", datetime.now(UTC))
    mine = product(images_db.conn, images_db.tenant("bharat"))
    by_key = await register_key(api, headers(images_db, "operator@bharat.test"), mine, key)
    assert by_key.status_code == 404
    assert "head" not in store.calls
    assert count(images_db.conn, "SELECT count(*) FROM images") == 0


@pytest.mark.endpoint("images_upload", case="not_found")
async def test_upload_not_found(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    response = await upload_file(api, headers(images_db), uuid.uuid4(), JPEG)
    assert response.status_code == 404 and response.json()["error"]["type"] == "NOT_FOUND"
    no_object = await register_key(
        api, headers(images_db), p, image_object_key(acme, p, uuid.uuid4())
    )
    assert no_object.status_code == 404


@pytest.mark.endpoint("images_upload", case="conflict")
async def test_upload_idem_key_other_body_409(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    assert (await upload_file(api, hdrs, p, JPEG)).status_code == 202
    response = await upload_file(api, hdrs, p, picture(65, 65))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_upload_other_member_same_key_409(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = str(uuid.uuid4())
    assert (await upload_file(api, headers(images_db, key=key), p, JPEG)).status_code == 202
    other = headers(images_db, "owner@acme.test", key=key)
    response = await upload_file(api, other, p, JPEG)
    assert response.status_code == 409


async def test_upload_replay_same_body_same_response(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    first = await upload_file(api, hdrs, p, JPEG)
    second = await upload_file(api, hdrs, p, JPEG)
    assert first.json() == second.json()
    assert len(store.objects) == 1
