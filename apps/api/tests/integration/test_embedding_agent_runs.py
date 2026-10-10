"""S16 T8 (AC3, DW2; invariant 7; CP12): every embedding call writes one `agent_runs` row, the
port's query embed included."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from .auth_support import World
from .brand_support import Conn, FakeEmbedder, agent_rows, failure
from .conftest import LOCAL_HOSTS
from .jobs_support import insert_job
from .test_pipeline_ports_a import build, chunk

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MODEL = "text-embedding-3-small"


def _ids(world: World) -> dict[str, object]:
    return {
        "tenant_id": world.tenant("acme"),
        "job_id": uuid.uuid4(),
        "attempt": 1,
        "request_id": "req-embed-1",
    }


async def test_embedding_agent_runs(products_world: World, auth_db: Conn) -> None:
    """DW2: one call → one row with tenant, agent, model, tokens, 6 dp cost and the ids."""
    from app.services.embeddings import HashEmbedder, embed_texts

    ids = _ids(products_world) | {"attempt": 2}
    batches = await embed_texts(HashEmbedder(MODEL), ["one two three", "four"], **ids)  # type: ignore[arg-type]
    assert len(batches) == 1 and len(batches[0].vectors) == 2
    (row,) = agent_rows(auth_db)
    assert row["tenant_id"] == ids["tenant_id"] and row["job_id"] == ids["job_id"]
    assert (row["agent_name"], row["trigger"], row["model_used"]) == ("embedding", "embed", MODEL)
    assert row["tokens_in"] == 4 and row["attempt"] == 2 and row["request_id"] == "req-embed-1"
    assert row["status"] == "completed" and row["error_report_json"] is None
    assert row["cost_usd"] == Decimal("0.000000")
    assert row["cost_usd"].as_tuple().exponent == -6


async def test_embedding_250_texts_3_calls_3_rows(products_world: World, auth_db: Conn) -> None:
    from app.services.embeddings import embed_texts

    embedder = FakeEmbedder()
    texts = [f"text {i}" for i in range(250)]
    batches = await embed_texts(embedder, texts, **_ids(products_world))  # type: ignore[arg-type]
    assert [len(c) for c in embedder.calls] == [100, 100, 50]
    assert sum(len(b.vectors) for b in batches) == 250
    assert len(agent_rows(auth_db)) == 3


@pytest.mark.parametrize(
    ("retryable", "timed_out", "status"),
    [(True, False, "failed"), (True, True, "timeout"), (False, False, "failed")],
)
async def test_embedding_failure_rows(
    products_world: World, auth_db: Conn, retryable: bool, timed_out: bool, status: str
) -> None:
    from app.services.embeddings import EmbeddingFailed, embed_texts

    embedder = FakeEmbedder(fail={1: failure(retryable=retryable, timed_out=timed_out)})
    with pytest.raises(EmbeddingFailed):
        await embed_texts(embedder, ["a"], **_ids(products_world))  # type: ignore[arg-type]
    (row,) = agent_rows(auth_db)
    assert row["status"] == status and row["model_used"] == MODEL
    report = row["error_report_json"]
    assert report["errors"][0]["source"] == "ai_model"
    assert report["retryable"] is retryable
    assert report["error_code"] == ("TIMEOUT" if timed_out else "MODEL_UNAVAILABLE")


async def test_embedding_row_has_no_text(products_world: World, auth_db: Conn) -> None:
    from app.services.embeddings import HashEmbedder, embed_texts, input_digest

    canary = "canary-brand-text-5e1f"
    await embed_texts(HashEmbedder(MODEL), [canary], **_ids(products_world))  # type: ignore[arg-type]
    (row,) = agent_rows(auth_db)
    assert canary not in repr(row)
    assert row["input_digest"] == input_digest([canary])


async def test_embed_query_one_row(products_world: World, auth_db: Conn) -> None:
    from app.services.embeddings import HashEmbedder, embed_query

    vector = await embed_query(HashEmbedder(MODEL), "kurta set", **_ids(products_world))  # type: ignore[arg-type]
    assert len(vector) == 1536
    assert len(agent_rows(auth_db)) == 1


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
async def test_embed_query_validation(products_world: World, auth_db: Conn, text: str) -> None:
    from app.services.embeddings import embed_query

    embedder = FakeEmbedder()
    with pytest.raises(ValueError, match="query"):
        await embed_query(embedder, text, **_ids(products_world))  # type: ignore[arg-type]
    assert embedder.calls == [] and agent_rows(auth_db) == []


async def test_embed_texts_empty_no_call(products_world: World, auth_db: Conn) -> None:
    from app.services.embeddings import embed_texts

    embedder = FakeEmbedder()
    assert await embed_texts(embedder, [], **_ids(products_world)) == []  # type: ignore[arg-type]
    assert embedder.calls == [] and agent_rows(auth_db) == []


async def test_query_embed_agent_run(products_world: World, auth_db: Conn) -> None:
    """PLAN AC3: the port's query embed writes one row with the job, its attempt and the
    request id, then returns the search result."""
    from app.core.request_context import request_context

    acme = products_world.tenant("acme")
    # any job row of the tenant carries the attempt the builder reads (a listing kind would also
    # need a listing row; the port reads only `attempt`)
    job = insert_job(auth_db, acme, kind="brand_embed", entity_type="brand_document", attempt=2)
    mine = chunk(auth_db, acme, "kurta set guidance")
    deps = await build(products_world, auth_db, job_id=job)
    with request_context("req-port-77"):
        found = await deps.retrieve_brand_chunks(acme, "kurta set", k=8)
    assert [c.id for c in found] == [mine]
    (row,) = agent_rows(auth_db)
    assert (row["tenant_id"], row["job_id"], row["attempt"]) == (acme, job, 2)
    assert row["request_id"] == "req-port-77" and row["status"] == "completed"


async def test_query_embed_failure_writes_row(products_world: World, auth_db: Conn) -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    acme = products_world.tenant("acme")
    deps = await build(products_world, auth_db)
    object.__setattr__(deps, "_embedder", FakeEmbedder(fail={1: failure(timed_out=True)}))
    with pytest.raises(SeamPortError) as info:
        await deps.retrieve_brand_chunks(acme, "kurta set")
    assert info.value.code is ErrorCode.TIMEOUT
    (row,) = agent_rows(auth_db)
    assert row["status"] == "timeout" and row["attempt"] == 1


async def test_query_embed_refused_input_no_row(products_world: World, auth_db: Conn) -> None:
    from app.services.pipeline_deps import SeamPortError

    deps = await build(products_world, auth_db)
    with pytest.raises(SeamPortError):
        await deps.retrieve_brand_chunks(products_world.tenant("acme"), "   ")
    assert agent_rows(auth_db) == []


async def test_malformed_openai_reply_writes_failed_row(
    products_world: World, auth_db: Conn
) -> None:
    """Audit JOBS-5: a 200 reply of an unusable shape (no `usage`) is `EmbeddingFailed`, and the
    call, which the provider may have billed, still writes its failed `agent_runs` row."""
    import httpx2
    from app.services.embeddings import EmbeddingFailed, embed_texts
    from tests.unit.test_embeddings_openai import Script, _embedder, _ok

    body = _ok(["a"])
    body.pop("usage")
    embedder = _embedder(Script(lambda r: httpx2.Response(200, json=body)))
    with pytest.raises(EmbeddingFailed):
        await embed_texts(embedder, ["a"], **_ids(products_world))  # type: ignore[arg-type]
    (row,) = agent_rows(auth_db)
    assert row["status"] == "failed" and row["model_used"] == MODEL
    assert row["error_report_json"]["error_code"] == "MODEL_UNAVAILABLE"
    assert row["error_report_json"]["retryable"] is False
