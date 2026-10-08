"""S16 T18 (AC0, AC4, AC6): `GET /api/v1/brand/documents` and `DELETE .../{document_id}`, as
app_rw through the real app."""

from __future__ import annotations

import uuid

import httpx
import pytest

from . import factories as f
from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/brand/documents"
REPORT = {
    "status": "failed",
    "error_code": "MODEL_UNAVAILABLE",
    "retryable": False,
    "attempts_used": 1,
    "max_attempts": 3,
    "errors": [],
    "request_id": "req-1",
}


def _doc(jw: JobWorld, slug: str = "acme", **columns: object) -> uuid.UUID:
    tenant = jw.tenant(slug)
    values: dict[str, object] = {
        "created_by": jw.owner(slug),
        "content_hash": uuid.uuid4().hex,
        "storage_key": f"{tenant}/brand-documents/{uuid.uuid4()}",
        "content_type": "text/markdown",
        "bytes": 10,
        **columns,
    }
    return f.brand_document(jw.conn, tenant, **values)


@pytest.mark.endpoint("brand_documents_list", case="success")
@pytest.mark.parametrize("role", ["owner", "operator", "reviewer", "admin"])
async def test_list_success(jobs_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    doc = _doc(jobs_db)
    response = await api.get(URL, headers=as_member(jobs_db, f"{role}@acme.test"))
    assert response.status_code == 200, response.text
    (item,) = response.json()["data"]
    assert item["id"] == str(doc) and item["error"] is None


async def test_list_keyset_walk(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    ids = {str(_doc(jobs_db)) for _ in range(120)}
    seen: list[str] = []
    sizes: list[int] = []
    cursor = None
    while True:
        params = {"limit": "50", **({"cursor": cursor} if cursor else {})}
        body = (
            await api.get(URL, params=params, headers=as_member(jobs_db, "owner@acme.test"))
        ).json()
        sizes.append(len(body["data"]))
        seen += [d["id"] for d in body["data"]]
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break
    assert sizes == [50, 50, 20] and set(seen) == ids and len(seen) == 120


async def test_list_error_on_failed(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    _doc(jobs_db, status="failed", error_report_json=REPORT)
    (item,) = (await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))).json()["data"]
    assert item["status"] == "failed" and item["error"]["error_code"] == "MODEL_UNAVAILABLE"


async def test_list_no_storage_key(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    _doc(jobs_db)
    response = await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))
    assert "storage_key" not in response.text and "brand-documents/" not in response.text
    assert "content_hash" not in response.text


@pytest.mark.endpoint("brand_documents_list", case="validation")
@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "201"}, {"cursor": "tampered!!"}])
async def test_list_validation_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, params: dict[str, str]
) -> None:
    response = await api.get(URL, params=params, headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("brand_documents_list", case="wrong_tenant")
async def test_list_other_tenant_never_listed(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    _doc(jobs_db, "bharat")
    response = await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 200 and response.json()["data"] == []


@pytest.mark.endpoint("brand_documents_list", case="missing_auth")
async def test_list_missing_auth_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(URL)).status_code == 401


@pytest.mark.endpoint("brand_documents_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("brand_documents_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("brand_documents_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_list_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    assert (await api.get(URL, headers=bad_token(jobs_db, variant))).status_code == 401


@pytest.mark.endpoint("brand_documents_delete", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_delete_success_204(jobs_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    from app.services.brand_storage import use_document_store

    from .brand_support import MemoryStore

    use_document_store(MemoryStore())
    try:
        doc = _doc(jobs_db)
        response = await api.delete(f"{URL}/{doc}", headers=as_member(jobs_db, f"{role}@acme.test"))
    finally:
        use_document_store(None)
    assert response.status_code == 204 and response.content == b""


async def test_delete_then_list_lacks(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    doc = _doc(jobs_db)
    owner = as_member(jobs_db, "owner@acme.test")
    assert (await api.delete(f"{URL}/{doc}", headers=owner)).status_code == 204
    assert (await api.get(URL, headers=owner)).json()["data"] == []
    again = await api.delete(f"{URL}/{doc}", headers=owner)
    assert again.status_code == 404


@pytest.mark.endpoint("brand_documents_delete", case="not_found")
async def test_delete_random_404(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.delete(
        f"{URL}/{uuid.uuid4()}", headers=as_member(jobs_db, "owner@acme.test")
    )
    assert response.status_code == 404 and response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("brand_documents_delete", case="wrong_tenant")
async def test_delete_cross_tenant_404_bytes_equal_random(
    jobs_db: JobWorld, api: httpx.AsyncClient
) -> None:
    theirs = _doc(jobs_db, "bharat")
    owner = as_member(jobs_db, "owner@acme.test")
    foreign = await api.delete(f"{URL}/{theirs}", headers={**owner, "X-Request-ID": "req-same"})
    random = await api.delete(
        f"{URL}/{uuid.uuid4()}", headers={**owner, "X-Request-ID": "req-same"}
    )
    assert foreign.status_code == random.status_code == 404
    assert foreign.content == random.content
    row = jobs_db.conn.execute(
        "SELECT deleted_at FROM brand_documents WHERE id = %s", [theirs]
    ).fetchone()
    assert row == (None,)


@pytest.mark.endpoint("brand_documents_delete", case="validation")
async def test_delete_non_uuid_422(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.delete(f"{URL}/not-a-uuid", headers=as_member(jobs_db, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("brand_documents_delete", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_delete_wrong_role_403(jobs_db: JobWorld, api: httpx.AsyncClient, role: str) -> None:
    response = await api.delete(
        f"{URL}/{_doc(jobs_db)}", headers=as_member(jobs_db, f"{role}@acme.test")
    )
    assert response.status_code == 403


@pytest.mark.endpoint("brand_documents_delete", case="missing_auth")
async def test_delete_missing_auth_401(jobs_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.delete(f"{URL}/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("brand_documents_delete", case="invalid_token", variant="expired")
@pytest.mark.endpoint("brand_documents_delete", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("brand_documents_delete", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_delete_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    assert (
        await api.delete(f"{URL}/{uuid.uuid4()}", headers=bad_token(jobs_db, variant))
    ).status_code == 401
