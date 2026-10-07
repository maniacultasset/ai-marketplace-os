"""S16 tester round 3 (F3/F4/F5, D1): with no document store installed, `POST /api/v1/brand/documents`
fails closed. It answers 500 INTERNAL_ERROR without naming the store, writes no document, job or
idempotency row, and the same Idempotency-Key succeeds once a store is installed."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest

from .auth_support import PREFIX
from .brand_support import GUIDE, MemoryStore
from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, as_member, count, jobs_db

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db"]
URL = f"{PREFIX}/brand/documents"


@pytest.fixture
def no_store() -> Iterator[None]:
    from app.services.brand_storage import use_document_store

    use_document_store(None)
    yield
    use_document_store(None)


def _rows(jw: JobWorld) -> tuple[int, int]:
    return (
        count(jw.conn, "SELECT count(*) FROM brand_documents"),
        count(jw.conn, "SELECT count(*) FROM job_runs"),
    )


async def _post(client: httpx.AsyncClient, jw: JobWorld, key: str) -> httpx.Response:
    return await client.post(
        URL,
        files={"file": ("guide.md", GUIDE, "application/octet-stream")},
        headers={**as_member(jw, "owner@acme.test"), "Idempotency-Key": key},
    )


async def test_upload_without_store_fails_closed_then_same_key_succeeds(
    jobs_db: JobWorld, no_store: None
) -> None:
    from app.main import create_app
    from app.services.brand_storage import use_document_store

    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    key = uuid.uuid4().hex
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        failed = await _post(client, jobs_db, key)
        assert failed.status_code == 500, failed.text
        assert failed.json()["error"]["type"] == "INTERNAL_ERROR"
        assert "document store" not in failed.text
        assert _rows(jobs_db) == (0, 0)

        memory = MemoryStore()
        use_document_store(memory)
        retried = await _post(client, jobs_db, key)
    assert retried.status_code == 202, retried.text
    assert _rows(jobs_db) == (1, 1)
