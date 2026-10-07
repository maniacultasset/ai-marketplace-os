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
