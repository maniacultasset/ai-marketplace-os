"""S16 brand test helpers: a scripted embedder, an in-memory document store, and readers for the
rows the brand code writes (read back on the admin connection, so a row is seen only if the code
under test committed it)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg

Conn = psycopg.Connection[tuple[Any, ...]]


def record(tenant_id: uuid.UUID, **over: Any) -> Any:
    from mkos_schemas.agent import AgentRunRecord

    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "request_id": "req-s16-test",
        "agent_name": "embedding",
        "trigger": "embed",
        "model_used": "text-embedding-3-small",
        "status": "completed",
        "started_at": now,
        "created_at": now,
        **over,
    }
    return AgentRunRecord(**values)


def agent_rows(conn: Conn, tenant_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM agent_runs"
    params: list[Any] = []
    if tenant_id is not None:
        query += " WHERE tenant_id = %s"
        params.append(tenant_id)
    cur = conn.execute(query + " ORDER BY started_at, id", params)
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


class FakeEmbedder:
    """Hash vectors behind a script: `fail` maps a call number (1-based) to an exception."""

    def __init__(
        self, model: str = "text-embedding-3-small", fail: dict[int, Exception] | None = None
    ):
        from app.services.embeddings import HashEmbedder

        self.model = model
        self._inner = HashEmbedder(model)
        self.fail = dict(fail or {})
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> Any:
        self.calls.append(list(texts))
        error = self.fail.get(len(self.calls))
        if error is not None:
            raise error
        return await self._inner.embed(texts)


def failure(*, retryable: bool = True, timed_out: bool = False) -> Exception:
    from app.services.embeddings import EmbeddingFailed
    from mkos_schemas.codes import ErrorCode

    code = ErrorCode.TIMEOUT if timed_out else ErrorCode.MODEL_UNAVAILABLE
    return EmbeddingFailed(code, retryable=retryable, timed_out=timed_out, cause="Scripted")


async def new_document(tenant_id: uuid.UUID, created_by: uuid.UUID, **over: Any) -> Any:
    """A live `uploaded` document row through the repository (committed)."""
    from app.repositories.uow import tenant_uow

    doc_id = over.pop("id", uuid.uuid4())
    values: dict[str, Any] = {
        "id": doc_id,
        "filename": "guide.md",
        "content_type": "text/markdown",
        "bytes_": 10,
        "content_hash": uuid.uuid4().hex * 2,
        "storage_key": f"{tenant_id}/brand-documents/{doc_id}",
        "job_id": uuid.uuid4(),
        "created_by": created_by,
        **over,
    }
    async with tenant_uow(tenant_id, user_id=created_by) as uow:
        await uow.brand_documents.create(**values)
    return values


def doc_row(conn: Conn, doc_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM brand_documents WHERE id = %s", [doc_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


def chunk_rows(
    conn: Conn, tenant_id: uuid.UUID, *, active: bool | None = True
) -> list[dict[str, Any]]:
    query = "SELECT id, type, text, source_type, source_id, chunk_index, content_hash, embedding_model, is_active FROM brand_chunks WHERE tenant_id = %s"
    params: list[Any] = [tenant_id]
    if active is not None:
        query += " AND is_active = %s"
        params.append(active)
    cur = conn.execute(query + " ORDER BY source_id, chunk_index, created_at", params)
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


class MemoryStore:
    """An in-memory `DocumentStore`; `fail_delete` makes `delete` raise."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.fail_delete = False
        self.puts = 0

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.puts += 1
        self.objects[(bucket, key)] = (data, content_type)

    async def get(self, bucket: str, key: str) -> bytes:
        from app.services.brand_storage import StorageNotFound

        try:
            return self.objects[(bucket, key)][0]
        except KeyError:
            raise StorageNotFound("the object does not exist") from None

    async def delete(self, bucket: str, key: str) -> None:
        if self.fail_delete:
            raise ConnectionError("store down")
        self.objects.pop((bucket, key), None)


def principal(world: Any, email: str = "owner@acme.test", slug: str = "acme") -> Any:
    from app.core.rbac import Principal

    return Principal(
        user_id=world.user(email),
        tenant_id=world.tenant(slug),
        role=email.split("@", 1)[0],  # type: ignore[arg-type]
        email=email,
        jti=str(uuid.uuid4()),
    )


GUIDE = (
    b"# Brand voice\n\nWe sound warm, plain and factual.\n\n"
    b"- Say kurta set, not kurta outfit.\n- Name the fabric first.\n\n"
    b"Compliance\n----------\n1. Never claim a product cures anything.\n"
)


def finish_jobs(conn: Conn, status: str) -> None:
    """Move queued jobs through `running` to `status` on the admin login (the guard's edges)."""
    from psycopg.types.json import Jsonb

    conn.execute(
        "UPDATE job_runs SET status = 'running', started_at = now(), "
        "lease_expires_at = now() + interval '1 hour' WHERE status = 'queued'"
    )
    report = Jsonb({"status": "failed"}) if status in ("failed", "dead_letter") else None
    conn.execute(
        "UPDATE job_runs SET status = %s, finished_at = now(), error_report_json = %s "
        "WHERE status = 'running'",
        [status, report],
    )
