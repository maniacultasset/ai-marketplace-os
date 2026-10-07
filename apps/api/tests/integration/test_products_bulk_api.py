"""S13 B8 (DW3, DW6, AC 15, AC 20; BACKEND-ARCHITECTURE:536-615; DATABASE.md:1610-1641):
`POST /api/v1/products/bulk` as app_rw: the upload order (peek, cap pre-check, parse, put, one
short job transaction), the file-level 422, idempotent replays over a fresh multipart boundary,
the live-import cap and the case matrix."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from app.adapters.storage import ObjectStorage, csv_bucket, get_storage
from app.services.imports import upload as upload_service
from mkos_schemas.errors import ErrorReport

from .conftest import LOCAL_HOSTS
from .imports_support import (
    BULK_URL,
    csv_bytes,
    headers,
    imports_db,
    one,
    row,
    upload,
)
from .jobs_support import JobWorld, bad_token, count, insert_job, queue_row

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["imports_db"]
GOOD = csv_bytes([row(1), row(2)])


def objects(tenant: uuid.UUID) -> list[str]:
    client: Any = get_storage()._client
    listed = client.list_objects_v2(Bucket=csv_bucket(), Prefix=f"{tenant}/imports/")
    return [o["Key"] for o in listed.get("Contents", [])]


def stored(jw: JobWorld, tenant: uuid.UUID) -> tuple[int, int]:
    return (
        count(jw.conn, "SELECT count(*) FROM imports WHERE tenant_id = %s", [tenant]),
        count(
            jw.conn,
            "SELECT count(*) FROM job_runs WHERE tenant_id = %s AND kind = 'bulk_import'",
            [tenant],
        ),
    )


def assert_import_failed(response: httpx.Response, field: str) -> ErrorReport:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["type"] == "IMPORT_FAILED"
    report = ErrorReport.model_validate(error["error_report"])
    assert report.error_code is not None and report.error_code.value == "IMPORT_FAILED"
    assert {e.field for e in report.errors} == {field}
    return report


# ---- the case matrix ----


@pytest.mark.endpoint("products_bulk", case="success")
@pytest.mark.parametrize("role", ["owner", "operator"])
async def test_products_bulk_success(
    imports_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    tenant = imports_db.tenant("acme")
    response = await upload(api, headers(imports_db, f"{role}@acme.test"), GOOD)
    assert response.status_code == 202, response.text
    body = response.json()
    import_id, job_id = uuid.UUID(body["import_id"]), uuid.UUID(body["job_id"])
    assert body["status"] == "queued"
    assert body["poll_url"] == f"/api/v1/jobs/{job_id}"
    found = imports_db.conn.execute(
        "SELECT storage_key, filename, status, job_id, created_by, imported_rows, failed_rows "
        "FROM imports WHERE id = %s",
        [import_id],
    ).fetchone()
    assert found == (
        f"{tenant}/imports/{import_id}.csv", "products.csv", "queued", job_id,
        imports_db.world.user(f"{role}@acme.test"), 0, 0,
    )  # fmt: skip
    job = imports_db.conn.execute(
        "SELECT kind, entity_type, entity_id, status, idempotency_key, queue_job_id "
        "FROM job_runs WHERE id = %s",
        [job_id],
    ).fetchone()
    assert job is not None and job[:5] == ("bulk_import", "import", import_id, "queued", None)
    assert queue_row(imports_db.conn, job[5])["args"] == {"job_run_id": str(job_id)}
    assert await get_storage().get(csv_bucket(), f"{tenant}/imports/{import_id}.csv") == GOOD


@pytest.mark.endpoint("products_bulk", case="validation")
@pytest.mark.parametrize("case", ["no_key", "no_file", "bad_header", "non_utf8", "10001_rows"])
async def test_products_bulk_validation(
    imports_db: JobWorld, api: httpx.AsyncClient, case: str
) -> None:
    tenant = imports_db.tenant("acme")
    hdrs = headers(imports_db)
    if case == "no_key":
        hdrs.pop("Idempotency-Key")
        response = await upload(api, hdrs, GOOD)
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    elif case == "no_file":
        response = await api.post(BULK_URL, headers=hdrs, files={"other": ("x.csv", GOOD)})
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "VALIDATION_FAILED"
    elif case == "bad_header":
        report = assert_import_failed(
            await upload(api, hdrs, b"seller_sku,colour\nA-1,red\n"), "file.header"
        )
        assert [e.error_code.value for e in report.errors] == [
            "HEADER_UNKNOWN_COLUMN", "HEADER_MISSING_COLUMN", "HEADER_MISSING_COLUMN",
        ]  # fmt: skip
    elif case == "non_utf8":
        data = "seller_sku,product_name,category\nA-1,Kurt\xe4,kurtas\n".encode("latin-1")
        assert_import_failed(await upload(api, hdrs, data), "file.row")
    else:
        body = "".join(f"A-{n},K,kurtas\n" for n in range(10_001))
        data = ("seller_sku,product_name,category\n" + body).encode()
        assert_import_failed(await upload(api, hdrs, data), "file.row")
    assert stored(imports_db, tenant) == (0, 0)
    assert objects(tenant) == []


@pytest.mark.endpoint("products_bulk", case="missing_auth")
async def test_products_bulk_missing_auth(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await upload(api, {"Idempotency-Key": str(uuid.uuid4())}, GOOD)
    assert response.status_code == 401
    assert stored(imports_db, imports_db.tenant("acme")) == (0, 0)


@pytest.mark.endpoint("products_bulk", case="invalid_token", variant="expired")
@pytest.mark.endpoint("products_bulk", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("products_bulk", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_products_bulk_invalid_token(
    imports_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(imports_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    assert (await upload(api, hdrs, GOOD)).status_code == 401


@pytest.mark.endpoint("products_bulk", case="wrong_role")
@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_products_bulk_wrong_role(
    imports_db: JobWorld, api: httpx.AsyncClient, role: str
) -> None:
    response = await upload(api, headers(imports_db, f"{role}@acme.test"), GOOD)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert stored(imports_db, imports_db.tenant("acme")) == (0, 0)


@pytest.mark.endpoint("products_bulk", case="wrong_tenant")
async def test_products_bulk_wrong_tenant(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    """The same key and file from two tenants: two imports, each under its own tenant prefix,
    and neither tenant's rows name the other."""
    key = str(uuid.uuid4())
    acme = await upload(api, headers(imports_db, "operator@acme.test", key), GOOD)
    bharat = await upload(api, headers(imports_db, "operator@bharat.test", key), GOOD)
    assert acme.status_code == bharat.status_code == 202
    assert acme.json()["import_id"] != bharat.json()["import_id"]
    for slug, response in (("acme", acme), ("bharat", bharat)):
        tenant = imports_db.tenant(slug)
        key_found = one(
            imports_db.conn,
            "SELECT storage_key FROM imports WHERE id = %s AND tenant_id = %s",
            [response.json()["import_id"], tenant],
        )
        assert key_found.startswith(f"{tenant}/imports/")
        assert objects(tenant) == [key_found]


@pytest.mark.endpoint("products_bulk", case="conflict")
@pytest.mark.parametrize("case", ["other_file", "other_member"])
async def test_products_bulk_conflict(
    imports_db: JobWorld, api: httpx.AsyncClient, case: str
) -> None:
    key = str(uuid.uuid4())
    assert (await upload(api, headers(imports_db, key=key), GOOD)).status_code == 202
    if case == "other_file":
        again = await upload(api, headers(imports_db, key=key), csv_bytes([row(9)]))
    else:
        again = await upload(api, headers(imports_db, "owner@acme.test", key), GOOD)
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    assert stored(imports_db, imports_db.tenant("acme")) == (1, 1)
    assert len(objects(imports_db.tenant("acme"))) == 1


# ---- the named cases ----


async def test_import_header_missing_422_nothing_stored(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = imports_db.tenant("acme")
    key = str(uuid.uuid4())
    data = b"seller_sku,category,main_image\nA-1,kurtas,https://x.example/a.jpg\n"
    first = await upload(api, headers(imports_db, key=key), data)
    report = assert_import_failed(first, "file.header")
    assert [(e.error_code.value, e.message) for e in report.errors] == [
        ("HEADER_MISSING_COLUMN", "missing column product_name")
    ]
    assert stored(imports_db, tenant) == (0, 0) and objects(tenant) == []
    assert count(imports_db.conn, "SELECT count(*) FROM procrastinate_jobs") == 0
    replay = await upload(api, headers(imports_db, key=key), data)
    assert replay.status_code == 422
    assert (
        replay.json()["error"]["error_report"]["errors"]
        == first.json()["error"]["error_report"]["errors"]
    )
    assert (
        one(imports_db.conn, "SELECT response_status FROM idempotency_keys WHERE key = %s", [key])
        == 422
    )


async def test_import_limits_10mib_plus_1_413(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    data = b"seller_sku,product_name,category\n" + b"A" * (10 * 1024 * 1024 + 1)
    response = await upload(api, headers(imports_db), data)
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"
    assert stored(imports_db, imports_db.tenant("acme")) == (0, 0)


async def test_import_limits_10001_rows_422(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    body = "".join(f"A-{n},K,kurtas,https://x.example/{n}.jpg\n" for n in range(10_001))
    data = ("seller_sku,product_name,category,main_image\n" + body).encode()
    report = assert_import_failed(await upload(api, headers(imports_db), data), "file.row")
    assert "10000" in report.errors[0].message


async def test_import_limits_non_utf8_422(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    data = b"seller_sku,product_name,category\nA-1,\xff\xfe,kurtas\n"
    report = assert_import_failed(await upload(api, headers(imports_db), data), "file.row")
    assert "UTF-8" in report.errors[0].message


async def test_import_object_key_tenant_prefix(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    response = await upload(api, headers(imports_db), GOOD, filename="../../other-tenant/x.csv")
    assert response.status_code == 202
    tenant = imports_db.tenant("acme")
    key = one(
        imports_db.conn,
        "SELECT storage_key FROM imports WHERE id = %s",
        [response.json()["import_id"]],
    )
    assert key == f"{tenant}/imports/{response.json()['import_id']}.csv"
    assert objects(imports_db.tenant("bharat")) == []


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\products.csv", "products.csv"),
        ("dir/sub/name.csv", "name.csv"),
        ("bad\u202ename.csv", "badname.csv"),
        ("..", "upload.csv"),
        ("x" * 300 + ".csv", "x" * 255),
    ],
)
async def test_import_filename_path_stripped(
    imports_db: JobWorld, api: httpx.AsyncClient, given: str, kept: str
) -> None:
    response = await upload(api, headers(imports_db), GOOD, filename=given)
    assert response.status_code == 202, response.text
    name = one(
        imports_db.conn,
        "SELECT filename FROM imports WHERE id = %s",
        [response.json()["import_id"]],
    )
    assert name == kept


def test_import_filename_control_chars_stripped() -> None:
    assert upload_service.safe_filename("a\x07b\u200bc\x00.csv") == "abc.csv"
    assert upload_service.safe_filename(None) == "upload.csv"
    assert upload_service.safe_filename("Cafe\u0301.csv") == "Caf\u00e9.csv"  # NFC


async def test_import_no_cell_value_logged(
    imports_db: JobWorld, api: httpx.AsyncClient, log_lines: Any
) -> None:
    capture = log_lines()
    canary = "CELL-CANARY-91e2"
    good = csv_bytes([row(1, product_name=canary)])
    bad = f"seller_sku,{canary}\nA-1,x\n".encode()
    assert (
        await upload(api, headers(imports_db), good, filename=f"{canary}.csv")
    ).status_code == 202
    response = await upload(api, headers(imports_db), bad)
    assert response.status_code == 422
    assert canary not in response.text
    text = capture.text()
    assert "import.accepted" in text and "import.refused" in text
    assert canary not in text


async def test_import_replay_same_file_same_ids(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Q3: each send has its own multipart boundary; the hash covers the file part only."""
    key = str(uuid.uuid4())
    first = await upload(api, headers(imports_db, key=key), GOOD, filename="a.csv")
    second = await upload(api, headers(imports_db, key=key), GOOD, filename="b.csv")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert stored(imports_db, imports_db.tenant("acme")) == (1, 1)
    assert len(objects(imports_db.tenant("acme"))) == 1  # the replay stores nothing


async def test_import_reupload_new_key_new_import(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    first = await upload(api, headers(imports_db), GOOD)
    second = await upload(api, headers(imports_db), GOOD)
    assert first.json()["import_id"] != second.json()["import_id"]
    assert first.json()["job_id"] != second.json()["job_id"]
    assert stored(imports_db, imports_db.tenant("acme")) == (2, 2)


async def test_products_bulk_expired_key_new_job_deferred(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    """Q11: the key expired after an earlier import; the same key starts a new import and a
    new job with its own queue entry (no job idempotency key collides for 90 days)."""
    key = str(uuid.uuid4())
    first = (await upload(api, headers(imports_db, key=key), GOOD)).json()
    imports_db.conn.execute(
        "UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = %s", [key]
    )
    imports_db.conn.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() WHERE id = %s",
        [first["job_id"]],
    )
    second = (await upload(api, headers(imports_db, key=key), GOOD)).json()
    assert second["import_id"] != first["import_id"] and second["job_id"] != first["job_id"]
    queue = [
        one(imports_db.conn, "SELECT queue_job_id FROM job_runs WHERE id = %s", [j])
        for j in (first["job_id"], second["job_id"])
    ]
    assert queue[0] != queue[1]
    assert queue_row(imports_db.conn, queue[1])["status"] == "todo"


class _HeldStorage(ObjectStorage):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.data: dict[str, bytes] = {}

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.entered.set()
        await self.release.wait()
        self.data[key] = data

    async def get(self, bucket: str, key: str) -> bytes:
        return self.data[key]


async def test_products_bulk_no_transaction_open_during_put(
    imports_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldStorage()
    monkeypatch.setattr(upload_service, "get_storage", lambda: held)
    request = asyncio.create_task(upload(api, headers(imports_db), GOOD))
    await asyncio.wait_for(held.entered.wait(), 10)
    idle = count(
        imports_db.conn,
        "SELECT count(*) FROM pg_stat_activity WHERE usename = 'app_rw' "
        "AND state LIKE 'idle in transaction%%' AND datname = current_database()",
    )
    held.release.set()
    response = await request
    assert response.status_code == 202
    assert idle == 0
    assert len(held.data) == 1


async def test_import_live_cap_per_tenant_429(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = imports_db.tenant("acme")
    live = [insert_job(imports_db.conn, tenant) for _ in range(2)]
    key = str(uuid.uuid4())
    response = await upload(api, headers(imports_db, key=key), GOOD)
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "RATE_LIMITED"
    assert response.json()["error"]["error_report"]["errors"][0]["source"] == "system"
    assert stored(imports_db, tenant) == (0, 2) and objects(tenant) == []
    assert (
        count(imports_db.conn, "SELECT count(*) FROM idempotency_keys WHERE key = %s", [key]) == 0
    )
    imports_db.conn.execute(
        "UPDATE job_runs SET status = 'cancelled', finished_at = now() WHERE id = %s", [live[0]]
    )
    assert (await upload(api, headers(imports_db, key=key), GOOD)).status_code == 202


async def test_import_cap_5_concurrent_never_exceeds_2(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    results = await asyncio.gather(
        *(upload(api, headers(imports_db), csv_bytes([row(n)])) for n in range(5))
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [202, 202, 429, 429, 429]
    tenant = imports_db.tenant("acme")
    assert stored(imports_db, tenant) == (2, 2)


async def test_import_cap_other_tenant_unaffected(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    for _ in range(2):
        insert_job(imports_db.conn, imports_db.tenant("bharat"))
    response = await upload(api, headers(imports_db), GOOD)
    assert response.status_code == 202


async def test_import_cap_counts_only_live(imports_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = imports_db.tenant("acme")
    for _ in range(2):
        job = insert_job(imports_db.conn, tenant)
        imports_db.conn.execute(
            "UPDATE job_runs SET status = 'running', attempt = 1, started_at = now(), "
            "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
            [job],
        )
        imports_db.conn.execute(
            "UPDATE job_runs SET status = 'succeeded', finished_at = now(), "
            "lease_expires_at = NULL WHERE id = %s",
            [job],
        )
    insert_job(imports_db.conn, tenant, kind="image_compliance", entity_type="image")
    assert (await upload(api, headers(imports_db), GOOD)).status_code == 202
