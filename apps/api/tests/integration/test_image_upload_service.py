"""S17b T9-T10 (AC2, AC7; PLAN D1, D8, D13, D14; CP2, CP3, CP7, CP8): `POST /api/v1/images/upload`
through the API as app_rw, on a fake object store: the storage-key branch, the multipart branch,
the main replacement, the per-product cap, the audit rows and the transaction boundaries."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from app.services import images as svc
from app.services.image_keys import image_object_key

from . import factories as f
from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    headers,
    image_db_row,
    image_row,
    images_db,
    picture,
    product,
    register_key,
    style_and_child,
    upload_file,
    use_storage,
)
from .jobs_support import JobWorld, count, job_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
JPEG = picture(64, 64)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    return fake


def put_object(
    store: FakeStorage, key: str, data: bytes = JPEG, age: timedelta = timedelta(0)
) -> None:
    store.objects[key] = (data, "image/jpeg", datetime.now(UTC) - age)


def new_key(jw: JobWorld, product_id: uuid.UUID, slug: str = "acme") -> str:
    return image_object_key(jw.tenant(slug), product_id, uuid.uuid4())


def rows(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM images")


def jobs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM job_runs WHERE kind = 'image_compliance'")


# ---- T9: the storage-key branch ----


async def test_register_key_creates_row_and_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued" and body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"
    row = image_db_row(images_db.conn, body["image_id"])
    assert row["storage_key"] == key and str(row["id"]) == key.rsplit("/", 1)[1]
    assert (row["status"], row["compliance_status"], row["bytes"]) == (
        "uploaded",
        "pending",
        len(JPEG),
    )
    job = job_row(images_db.conn, uuid.UUID(body["job_id"]))
    assert (job["entity_type"], str(job["entity_id"]), job["kind"]) == (
        "image",
        body["image_id"],
        "image_compliance",
    )
    assert job["payload_json"] == {"image_id": body["image_id"]}


@pytest.mark.parametrize("variant", ["plain", "tid_prefix_lookalike"])
async def test_register_foreign_prefix_404_no_head(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, variant: str
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    if variant == "plain":
        key = new_key(images_db, p, "bharat")
    else:  # another tenant id that shares the caller's first characters
        other = uuid.UUID(str(acme)[:8] + str(uuid.uuid4())[8:])
        key = image_object_key(other, p, uuid.uuid4())
    put_object(store, key)
    response = await register_key(api, headers(images_db), p, key)
    missing = await register_key(api, headers(images_db), p, new_key(images_db, p))
    assert response.status_code == missing.status_code == 404
    assert response.json()["error"]["message"] == missing.json()["error"]["message"]
    assert store.calls.count("head") == 1  # the missing key only
    assert rows(images_db) == 0


async def test_register_key_product_mismatch_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p, q = product(images_db.conn, acme), product(images_db.conn, acme)
    key = new_key(images_db, q)
    put_object(store, key)
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "storage_key"]


@pytest.mark.parametrize(
    "shape",
    [
        "{t}/{p}/images/{i}.jpg",
        "{T}/{p}/images/{i}",
        "{t}/{p}/imports/{i}",
        "{t}/{p}/images/{i}/x",
        "{t}/images/{i}",
    ],
    ids=["ext_jpg", "uppercase", "other_folder", "extra_segment", "short"],
)
async def test_register_malformed_key_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, shape: str
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    key = shape.format(t=acme, T=str(acme).upper(), p=p, i=uuid.uuid4())
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 422
    assert store.calls == []


async def test_register_key_without_object_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await register_key(api, headers(images_db), p, new_key(images_db, p))
    assert response.status_code == 404 and rows(images_db) == 0


async def test_register_object_over_cap_413(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key, b"x" * (svc.MAX_IMAGE_BYTES + 1))
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"
    assert rows(images_db) == 0


async def test_register_stale_object_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key, age=timedelta(hours=23, minutes=1))
    response = await register_key(api, headers(images_db), p, key)
    assert response.status_code == 422 and rows(images_db) == 0


async def test_register_same_key_new_idem_key_same_image(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    first = await register_key(api, headers(images_db), p, key)
    second = await register_key(api, headers(images_db), p, key)
    assert first.status_code == second.status_code == 202
    assert first.json()["image_id"] == second.json()["image_id"]
    assert first.json()["job_id"] == second.json()["job_id"]
    assert (rows(images_db), jobs(images_db)) == (1, 1)


async def test_register_same_key_other_slot_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    assert (await register_key(api, headers(images_db), p, key)).status_code == 202
    response = await register_key(api, headers(images_db), p, key, slot="secondary")
    assert response.status_code == 422


async def test_register_deleted_key_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    body = (await register_key(api, headers(images_db), p, key)).json()
    images_db.conn.execute("UPDATE images SET deleted_at = now() WHERE id = %s", [body["image_id"]])
    assert (await register_key(api, headers(images_db), p, key)).status_code == 422


async def test_register_two_concurrent_same_key_one_row(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    a, b = await asyncio.gather(
        register_key(api, headers(images_db), p, key), register_key(api, headers(images_db), p, key)
    )
    assert a.status_code == b.status_code == 202, (a.text, b.text)
    assert a.json()["image_id"] == b.json()["image_id"]
    assert (rows(images_db), jobs(images_db)) == (1, 1)


async def test_register_replay_same_idem_key(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    hdrs = headers(images_db)
    first = await register_key(api, hdrs, p, key)
    second = await register_key(api, hdrs, p, key)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert (rows(images_db), jobs(images_db)) == (1, 1)


async def test_register_idem_key_other_body_409(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key, other = new_key(images_db, p), new_key(images_db, p)
    put_object(store, key)
    put_object(store, other)
    hdrs = headers(images_db)
    assert (await register_key(api, hdrs, p, key)).status_code == 202
    response = await register_key(api, hdrs, p, other)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"


async def test_register_colourway_of_other_product_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    style, other = product(images_db.conn, acme, "style"), product(images_db.conn, acme, "style")
    cw = f.colourway(images_db.conn, acme, other)
    key = new_key(images_db, style)
    put_object(store, key)
    response = await register_key(api, headers(images_db), style, key, colourway_id=cw)
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "colourway_id"]


async def test_register_size_chart_on_child_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    _style, child = style_and_child(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, child)
    put_object(store, key)
    response = await register_key(api, headers(images_db), child, key, slot="size_chart")
    assert response.status_code == 422


def _idle_in_transaction(jw: JobWorld) -> int:
    return count(
        jw.conn,
        "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
        "AND datname = current_database() AND state LIKE 'idle in transaction%%'",
    )


async def test_register_no_transaction_open_during_head(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    key = new_key(images_db, p)
    put_object(store, key)
    seen: list[int] = []
    real = store.head

    async def head(*a: Any, **k: Any) -> Any:
        seen.append(_idle_in_transaction(images_db))
        return await real(*a, **k)

    monkeypatch.setattr(store, "head", head)
    assert (await register_key(api, headers(images_db), p, key)).status_code == 202
    assert seen == [0]


# ---- T10: the multipart branch, main replacement, caps, audit ----


async def test_upload_multipart_creates_row_and_job(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    response = await upload_file(api, headers(images_db), p, JPEG)
    assert response.status_code == 202, response.text
    row = image_db_row(images_db.conn, response.json()["image_id"])
    assert row["storage_key"] == image_object_key(acme, p, row["id"])
    assert store.objects[row["storage_key"]][1] == "application/octet-stream"
    assert jobs(images_db) == 1


async def test_upload_empty_file_422(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, b"")
    assert response.status_code == 422 and store.objects == {}


async def test_upload_over_cap_413_nothing_stored(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "MAX_IMAGE_BYTES", 1000)  # the request cap itself is S3's (T11)
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await upload_file(api, headers(images_db), p, b"x" * 1001)
    assert response.status_code == 413
    assert store.objects == {} and rows(images_db) == 0


async def test_upload_declared_type_ignored(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    response = await api.post(
        "/api/v1/images/upload",
        headers=headers(images_db),
        data={"product_id": str(p), "slot": "main"},
        files={"file": ("x.html", JPEG, "text/html")},
    )
    assert response.status_code == 202
    assert [o[1] for o in store.objects.values()] == ["application/octet-stream"]


def audit_rows(jw: JobWorld, image_id: Any) -> list[tuple[Any, ...]]:
    return jw.conn.execute(
        "SELECT action, before, after, actor_user_id, request_id FROM audit_log "
        "WHERE entity_type = 'image' AND entity_id = %s ORDER BY occurred_at, action",
        [image_id],
    ).fetchall()


async def test_image_main_replaced(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    first = (await upload_file(api, headers(images_db), p, JPEG)).json()
    second = (await upload_file(api, headers(images_db), p, picture(80, 80))).json()
    old = image_db_row(images_db.conn, first["image_id"])
    assert old["deleted_at"] is not None
    assert old["storage_key"] in store.objects  # versions are kept (spec L10193)
    live = count(
        images_db.conn, "SELECT count(*) FROM images WHERE slot = 'main' AND deleted_at IS NULL"
    )
    assert live == 1
    replaced = [r for r in audit_rows(images_db, first["image_id"]) if r[0] == "image.replaced"]
    assert len(replaced) == 1 and replaced[0][1] == {"deleted_at": None}
    uploaded = [r for r in audit_rows(images_db, second["image_id"]) if r[0] == "image.uploaded"]
    assert len(uploaded) == 1
    assert uploaded[0][2] == {"slot": "main", "status": "uploaded"}
    assert uploaded[0][3] == images_db.world.user("operator@acme.test") and uploaded[0][4]


async def test_image_main_per_colourway(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    style = product(images_db.conn, acme, "style")
    cw1, cw2 = f.colourway(images_db.conn, acme, style), f.colourway(images_db.conn, acme, style)
    for cw in (cw1, cw2, None):
        form: dict[str, Any] = {} if cw is None else {"colourway_id": cw}
        assert (await upload_file(api, headers(images_db), style, JPEG, **form)).status_code == 202
    live = count(
        images_db.conn, "SELECT count(*) FROM images WHERE slot = 'main' AND deleted_at IS NULL"
    )
    assert live == 3


async def test_image_positions_cp7(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    ids = [
        (await upload_file(api, headers(images_db), p, JPEG, slot="secondary")).json()["image_id"]
        for _ in range(3)
    ]
    assert [image_db_row(images_db.conn, i)["position"] for i in ids] == [0, 1, 2]


async def test_image_cap_50(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    for _ in range(50):
        image_row(images_db.conn, acme, p)
    image_row(images_db.conn, acme, p, deleted_at=datetime.now(UTC))
    before = rows(images_db)
    response = await register_key(
        api, headers(images_db), p, _put_new(images_db, store, p), slot="secondary"
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["loc"] == ["body", "images"]
    assert rows(images_db) == before


def _put_new(jw: JobWorld, store: FakeStorage, p: uuid.UUID) -> str:
    key = new_key(jw, p)
    put_object(store, key)
    return key


async def test_image_cap_concurrent_never_exceeds(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    acme = images_db.tenant("acme")
    p = product(images_db.conn, acme)
    for _ in range(49):
        image_row(images_db.conn, acme, p)
    keys = [_put_new(images_db, store, p) for _ in range(5)]
    results = await asyncio.gather(
        *(register_key(api, headers(images_db), p, k, slot="secondary") for k in keys)
    )
    assert sorted(r.status_code for r in results) == [202, 422, 422, 422, 422]
    assert count(images_db.conn, "SELECT count(*) FROM images WHERE deleted_at IS NULL") == 50


async def test_images_upload_no_transaction_during_put(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    seen: list[int] = []
    real = store.put

    async def put(*a: Any, **k: Any) -> None:
        seen.append(_idle_in_transaction(images_db))
        await real(*a, **k)

    monkeypatch.setattr(store, "put", put)
    assert (await upload_file(api, headers(images_db), p, JPEG)).status_code == 202
    assert seen == [0]


async def test_upload_failure_after_put_leaves_orphan_only(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.jobs import JobRunRepository

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(JobRunRepository, "enqueue", boom)
    p = product(images_db.conn, images_db.tenant("acme"))
    hdrs = headers(images_db)
    response = await upload_file(api, hdrs, p, JPEG)
    assert response.status_code == 500
    assert (rows(images_db), jobs(images_db)) == (0, 0)
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0
    assert len(store.objects) == 1


async def test_upload_audit_failure_rolls_back(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.identity import AuditRepository

    p = product(images_db.conn, images_db.tenant("acme"))
    first = (await upload_file(api, headers(images_db), p, JPEG)).json()

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("audit failed")

    monkeypatch.setattr(AuditRepository, "write", boom)
    assert (await upload_file(api, headers(images_db), p, JPEG)).status_code == 500
    assert image_db_row(images_db.conn, first["image_id"])["deleted_at"] is None
    assert (rows(images_db), jobs(images_db)) == (1, 1)


async def test_upload_audit_rows_have_no_personal_data(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    p = product(images_db.conn, images_db.tenant("acme"))
    await upload_file(api, headers(images_db), p, JPEG)
    await upload_file(api, headers(images_db), p, JPEG)
    for before, after in images_db.conn.execute(
        "SELECT before::text, after::text FROM audit_log WHERE entity_type = 'image'"
    ).fetchall():
        assert "@" not in (before or "") + (after or "")
