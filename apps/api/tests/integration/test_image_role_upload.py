"""SI12 T7 (AC2, AC3): uploads with a photo role, by multipart and at product create."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from app.services.image_keys import image_object_key

from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    headers,
    image_db_row,
    images_db,
    picture,
    product,
    register_key,
    upload_file,
    use_storage,
)
from .jobs_support import JobWorld, as_member, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
JPEG = picture(48, 48)
PRODUCTS = "/api/v1/products"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


def _cleared(jw: JobWorld, image_id: Any) -> list[tuple[Any, ...]]:
    return jw.conn.execute(
        "SELECT before, after FROM audit_log WHERE entity_type = 'image' AND entity_id = %s "
        "AND action = 'image.role_cleared'",
        [image_id],
    ).fetchall()


def _loc(response: httpx.Response) -> list[Any]:
    return [tuple(d["loc"]) for d in response.json()["error"].get("details", [])]


def _code(response: httpx.Response) -> str:
    code: str = response.json()["error"]["type"]
    return code


async def _detail(jw: JobWorld, api: httpx.AsyncClient, product_id: Any) -> list[dict[str, Any]]:
    response = await api.get(
        f"{PRODUCTS}/{product_id}", headers=as_member(jw, "reviewer@acme.test")
    )
    assert response.status_code == 200, response.text
    details: list[dict[str, Any]] = response.json()["image_details"]
    return details


async def test_multipart_back_stores_the_role(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, JPEG, "secondary", role="back")
    assert response.status_code == 202, response.text
    image_id = response.json()["image_id"]
    assert image_db_row(images_db.conn, image_id)["role"] == "back"
    details = await _detail(images_db, api, p)
    assert [(d["slot"], d["role"]) for d in details] == [("secondary", "back")]


@pytest.mark.parametrize(("slot", "role"), [("main", "back"), ("size_chart", "close_up")])
async def test_role_on_other_slot_422_at_role(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, slot: str, role: str
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, JPEG, slot, role=role)
    assert response.status_code == 422, response.text
    assert _code(response) == "VALIDATION_FAILED"
    assert any("role" in str(f) for f in _loc(response)), response.text
    assert count(images_db.conn, "SELECT count(*) FROM images WHERE product_id = %s", [p]) == 0


async def test_unknown_role_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, JPEG, "secondary", role="front")
    assert response.status_code == 422, response.text
    assert any("role" in str(f) for f in _loc(response)), response.text


async def test_second_back_takes_the_role(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    first = (await upload_file(api, headers(images_db), p, JPEG, "secondary", role="back")).json()
    close = await upload_file(
        api, headers(images_db), p, picture(50, 50), "secondary", role="close_up"
    )
    assert close.status_code == 202
    second = await upload_file(
        api, headers(images_db), p, picture(52, 52), "secondary", role="back"
    )
    assert second.status_code == 202, second.text
    old = image_db_row(images_db.conn, first["image_id"])
    assert (old["role"], old["slot"], old["deleted_at"]) == (None, "secondary", None)
    assert image_db_row(images_db.conn, second.json()["image_id"])["role"] == "back"
    assert image_db_row(images_db.conn, close.json()["image_id"])["role"] == "close_up"
    audits = _cleared(images_db, first["image_id"])
    assert len(audits) == 1 and audits[0][1] == {"slot": "secondary"}
    roles = sorted(str(d["role"]) for d in await _detail(images_db, api, p))
    assert roles == ["None", "back", "close_up"]


async def test_replay_and_key_reuse(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    first = await upload_file(api, hdrs, p, JPEG, "secondary", role="back")
    again = await upload_file(api, hdrs, p, JPEG, "secondary", role="back")
    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json() == first.json()
    other = await upload_file(api, hdrs, p, JPEG, "secondary", role="close_up")
    assert other.status_code == 409, other.text
    assert _code(other) == "IDEMPOTENCY_KEY_REUSED"
    plain = await upload_file(api, hdrs, p, JPEG, "secondary")
    assert plain.status_code == 409


async def test_same_storage_key_with_another_role_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = image_object_key(acme, p, uuid.uuid4())
    await store.put("images", key, JPEG, "image/jpeg")
    first = await register_key(api, headers(images_db), p, key, "secondary", role="back")
    assert first.status_code == 202, first.text
    replay = await register_key(api, headers(images_db), p, key, "secondary", role="back")
    assert replay.status_code == 202 and replay.json()["image_id"] == first.json()["image_id"]
    other = await register_key(api, headers(images_db), p, key, "secondary", role="close_up")
    assert other.status_code == 422, other.text
    assert any("storage_key" in str(f) for f in _loc(other))


@pytest.mark.parametrize("member", ["reviewer", "admin"])
async def test_member_without_import_403(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, member: str
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db, f"{member}@acme.test")
    response = await upload_file(api, hdrs, p, JPEG, "secondary", role="back")
    assert response.status_code == 403


async def test_parallel_backs_leave_one_holder(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    answers = await asyncio.gather(
        *(
            upload_file(api, headers(images_db), p, picture(40 + i, 40), "secondary", role="back")
            for i in range(4)
        )
    )
    assert [a.status_code for a in answers] == [202] * 4, [a.text for a in answers]
    holders = count(
        images_db.conn,
        "SELECT count(*) FROM images WHERE product_id = %s AND role = 'back' "
        "AND deleted_at IS NULL",
        [p],
    )
    assert holders == 1


# ---- product create ----


def _key(tenant: uuid.UUID, product_id: uuid.UUID) -> str:
    return image_object_key(tenant, product_id, uuid.uuid4())


def _body(product_id: uuid.UUID, uploads: list[dict[str, Any]], sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "title": "Tee",
        "category": "t_shirts",
        "id": str(product_id),
        "image_uploads": uploads,
    }


async def test_create_with_the_four_photos(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    plan = [("main", None), ("secondary", "back"), ("secondary", "close_up"), ("size_chart", None)]
    uploads = []
    for i, (slot, role) in enumerate(plan):
        key = _key(tenant, pid)
        await store.put("images", key, picture(40 + i, 40), "image/jpeg")
        uploads.append({"storage_key": key, "slot": slot, **({"role": role} if role else {})})
    response = await api.post(
        PRODUCTS,
        json=_body(pid, uploads, "SI12-4"),
        headers=as_member(images_db, "operator@acme.test"),
    )
    assert response.status_code == 201, response.text
    got = sorted((d["slot"], str(d["role"])) for d in response.json()["image_details"])
    assert got == sorted((s, str(r)) for s, r in plan)


@pytest.mark.parametrize(
    "uploads",
    [
        [("secondary", "back"), ("secondary", "back")],
        [("main", None), ("main", "back")],
    ],
)
async def test_create_bad_roles_422(
    images_db: JobWorld,
    api: httpx.AsyncClient,
    store: FakeStorage,
    uploads: list[tuple[str, str | None]],
) -> None:
    tenant, pid = images_db.tenant("acme"), uuid.uuid4()
    body_uploads = []
    for i, (slot, role) in enumerate(uploads):
        key = _key(tenant, pid)
        await store.put("images", key, picture(40 + i, 40), "image/jpeg")
        body_uploads.append({"storage_key": key, "slot": slot, **({"role": role} if role else {})})
    response = await api.post(
        PRODUCTS,
        json=_body(pid, body_uploads, f"SI12-BAD-{uuid.uuid4().hex[:6]}"),
        headers=as_member(images_db, "operator@acme.test"),
    )
    assert response.status_code == 422, response.text
    assert any("image_uploads" in str(f) for f in _loc(response)), response.text
    assert count(images_db.conn, "SELECT count(*) FROM products WHERE id = %s", [pid]) == 0
