"""S16 T17 (AC0, AC1, AC6): `POST /api/v1/brand/documents`, as app_rw through the real app."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

from .auth_support import PREFIX
from .brand_support import GUIDE, MemoryStore
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, bad_token, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/brand/documents"


@pytest.fixture
def store() -> Iterator[MemoryStore]:
    from app.services.brand_storage import use_document_store

    memory = MemoryStore()
    use_document_store(memory)
    yield memory
    use_document_store(None)


async def _post(
    api: httpx.AsyncClient,
    jw: JobWorld,
    data: bytes = GUIDE,
    *,
    email: str = "owner@acme.test",
    key: str | None = None,
    name: str = "guide.md",
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    base = as_member(jw, email) if headers is None else headers
    return await api.post(
        URL,
        files={"file": (name, data, "application/octet-stream")},
        data=form or {},
        headers={**base, "Idempotency-Key": key or uuid.uuid4().hex},
    )


def _docs(jw: JobWorld) -> int:
    return count(jw.conn, "SELECT count(*) FROM brand_documents")


@pytest.mark.endpoint("brand_documents_upload", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_upload_success_202(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, role: str
) -> None:
    response = await _post(api, jobs_db, email=f"{role}@acme.test", form={"type": "compliance"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued" and body["poll_url"] == f"/api/v1/jobs/{body['job_id']}"
    payload = jobs_db.conn.execute("SELECT payload_json FROM job_runs").fetchone()
    assert payload is not None and payload[0]["chunk_type"] == "compliance"


async def test_upload_then_job_get(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    job = (await _post(api, jobs_db)).json()["job_id"]
    response = await api.get(
        f"{PREFIX}/jobs/{job}", headers=as_member(jobs_db, "reviewer@acme.test")
    )
    assert response.status_code == 200 and response.json()["kind"] == "brand_embed"


@pytest.mark.endpoint("brand_documents_upload", case="validation")
@pytest.mark.parametrize(
    ("data", "name", "form"),
    [
        (b"plain", "guide.pdf", {}),
        (b"", "guide.md", {}),
        (b"caf\xe9", "guide.txt", {}),
        (b"a\x00b", "guide.txt", {}),
        (b"# heading only", "guide.md", {}),
        ("\n".join(f"- u{i}" for i in range(501)).encode(), "guide.md", {}),
        (GUIDE, "guide.md", {"type": "example"}),
        (b'{"secret": "x"}', "guide.md", {}),
        (("Atzr|" + "IwEBIA1b2c3d4e5f6g7h8i9j0").encode(), "guide.md", {}),
    ],
)
async def test_upload_validation_422(
    jobs_db: JobWorld,
    api: httpx.AsyncClient,
    store: MemoryStore,
    data: bytes,
    name: str,
    form: dict[str, str],
) -> None:
    response = await _post(api, jobs_db, data, name=name, form=form)
    assert response.status_code == 422 and response.json()["error"]["type"] == "VALIDATION_FAILED"
    assert _docs(jobs_db) == 0 and store.puts == 0


async def test_upload_no_key_or_no_file_422(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    no_key = await api.post(
        URL, files={"file": ("g.md", GUIDE)}, headers=as_member(jobs_db, "owner@acme.test")
    )
    no_file = await api.post(
        URL,
        data={"type": "tone_rule"},
        headers={**as_member(jobs_db, "owner@acme.test"), "Idempotency-Key": "k1"},
    )
    assert (no_key.status_code, no_file.status_code) == (422, 422)


async def test_upload_body_over_cap_413(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    response = await _post(api, jobs_db, b"a" * (10 * 1024 * 1024 + 1))
    assert response.status_code == 413 and response.json()["error"]["type"] == "PAYLOAD_TOO_LARGE"
    assert _docs(jobs_db) == 0


async def test_upload_live_cap_429(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    await _post(api, jobs_db, b"one")
    await _post(api, jobs_db, b"two")
    response = await _post(api, jobs_db, b"three")
    assert response.status_code == 429 and response.json()["error"]["type"] == "RATE_LIMITED"


@pytest.mark.endpoint("brand_documents_upload", case="conflict")
async def test_upload_same_key_other_file_409(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    assert (await _post(api, jobs_db, key="k-409")).status_code == 202
    response = await _post(api, jobs_db, b"another file", key="k-409")
    assert (
        response.status_code == 409 and response.json()["error"]["type"] == "IDEMPOTENCY_KEY_REUSED"
    )


async def test_upload_replay_same_key_same_body(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    first = await _post(api, jobs_db, key="k-replay")
    second = await _post(api, jobs_db, key="k-replay", name="renamed.md")
    assert second.status_code == 202 and second.json() == first.json()
    assert _docs(jobs_db) == 1


async def test_upload_other_member_same_key_409(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    await _post(api, jobs_db, key="k-member")
    response = await _post(api, jobs_db, key="k-member", email="admin@acme.test")
    assert response.status_code == 409


@pytest.mark.endpoint("brand_documents_upload", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_upload_wrong_role_403(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, role: str
) -> None:
    response = await _post(api, jobs_db, email=f"{role}@acme.test")
    assert response.status_code == 403 and _docs(jobs_db) == 0


@pytest.mark.endpoint("brand_documents_upload", case="wrong_tenant")
async def test_upload_lands_under_own_tenant(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    await _post(api, jobs_db, email="owner@bharat.test")
    (key,) = list(store.objects)
    assert key[1].startswith(f"{jobs_db.tenant('bharat')}/brand-documents/")
    listing = await api.get(URL, headers=as_member(jobs_db, "owner@acme.test"))
    assert listing.json()["data"] == []


@pytest.mark.endpoint("brand_documents_upload", case="missing_auth")
async def test_upload_missing_auth_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore
) -> None:
    assert (await _post(api, jobs_db, headers={})).status_code == 401


@pytest.mark.endpoint("brand_documents_upload", case="invalid_token", variant="expired")
@pytest.mark.endpoint("brand_documents_upload", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("brand_documents_upload", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_upload_invalid_token_401(
    jobs_db: JobWorld, api: httpx.AsyncClient, store: MemoryStore, variant: str
) -> None:
    assert (await _post(api, jobs_db, headers=bad_token(jobs_db, variant))).status_code == 401
