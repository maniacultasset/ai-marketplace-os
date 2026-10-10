"""S23 T15 (AC8; D4): `job_runs.traceparent` is stamped from the span current at enqueue time,
NULL outside any span, and refused by the CHECK when malformed."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .ops_support import job_run

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

Conn = psycopg.Connection[tuple[Any, ...]]
VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def _settings(sampler: str = "always_on") -> Any:
    from app.core.config import Settings

    return Settings(_env_file=None, app_env="local", otel_traces_sampler=sampler)


@pytest.fixture
def traced() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    from app.core.observability import init_tracing

    exporter = InMemorySpanExporter()
    yield init_tracing(_settings(), exporter=exporter), exporter


def _hex(span: Any) -> tuple[str, str]:
    context = span.get_span_context()
    return f"{context.trace_id:032x}", f"{context.span_id:016x}"


async def _enqueue(tenant: uuid.UUID) -> Any:
    from app.repositories.jobs import EnqueueSpec, JobRunRepository
    from app.repositories.transactions import unit_of_work
    from mkos_schemas.enums import JobKind

    async with unit_of_work(tenant) as session:
        made = await JobRunRepository(session, tenant).enqueue(
            EnqueueSpec(
                kind=JobKind.BULK_IMPORT,
                entity_type="import",
                entity_id=uuid.uuid4(),
                created_by=None,
                payload={"import_id": str(uuid.uuid4())},
            )
        )
        return made.job


def _stored(conn: Conn, job_id: uuid.UUID) -> str | None:
    row = conn.execute("SELECT traceparent FROM job_runs WHERE id = %s", [job_id]).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


async def test_job_traceparent_stamped_inside_span(
    gen_world: GenWorld, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    provider, _ = traced
    with provider.get_tracer("t").start_as_current_span("request") as span:
        job = await _enqueue(gen_world.tenant("acme"))
    trace_id, span_id = _hex(span)
    assert _stored(gen_world.conn, job.id) == f"00-{trace_id}-{span_id}-01"


async def test_job_traceparent_null_outside_span(gen_world: GenWorld) -> None:
    job = await _enqueue(gen_world.tenant("acme"))
    assert _stored(gen_world.conn, job.id) is None


async def test_job_traceparent_explicit_value_wins(
    gen_world: GenWorld, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    from app.db.models.ops import JobRun
    from app.repositories.jobs import JobRunRepository
    from app.repositories.transactions import unit_of_work

    provider, _ = traced
    acme = gen_world.tenant("acme")
    with provider.get_tracer("t").start_as_current_span("request"):
        async with unit_of_work(acme) as session:
            job = JobRun(
                kind="bulk_import",
                entity_type="import",
                entity_id=uuid.uuid4(),
                payload_json={},
                correlation_id=uuid.uuid4(),
                traceparent=VALID,
            )
            await JobRunRepository(session, acme).add(job)
    assert _stored(gen_world.conn, job.id) == VALID


async def test_job_traceparent_unsampled_span_flags_00(gen_world: GenWorld) -> None:
    from app.core.observability import init_tracing

    provider = init_tracing(_settings("always_off"))
    with provider.get_tracer("t").start_as_current_span("request") as span:
        assert not span.is_recording()
        job = await _enqueue(gen_world.tenant("acme"))
    trace_id, span_id = _hex(span)
    assert _stored(gen_world.conn, job.id) == f"00-{trace_id}-{span_id}-00"


async def test_job_traceparent_check_refuses_malformed(gen_world: GenWorld) -> None:
    from app.db.models.ops import JobRun
    from app.repositories.jobs import JobRunRepository
    from app.repositories.transactions import unit_of_work

    acme = gen_world.tenant("acme")
    with pytest.raises(Exception) as caught:
        async with unit_of_work(acme) as session:
            await JobRunRepository(session, acme).add(
                JobRun(
                    kind="bulk_import",
                    entity_type="import",
                    entity_id=uuid.uuid4(),
                    payload_json={},
                    correlation_id=uuid.uuid4(),
                    traceparent=VALID.upper(),
                )
            )
    chain: list[BaseException] = []
    error: BaseException | None = caught.value
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    states = {getattr(getattr(e, "orig", None), "sqlstate", None) for e in chain}
    names = {getattr(e, "sqlstate", None) for e in chain}
    assert "23514" in states | names, chain


async def test_job_traceparent_null_for_cli_requeue(
    gen_world: GenWorld, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    """A staff requeue runs outside any request span: its new row has no traceparent, even when
    the old one had one."""
    from app.repositories.jobs import JobRunRepository
    from app.repositories.transactions import unit_of_work

    acme = gen_world.tenant("acme")
    old = job_run(
        gen_world.conn,
        acme,
        status="failed",
        attempt=1,
        started_at="2026-10-06T00:00:00Z",
        finished_at="2026-10-06T01:00:00Z",
        error_report_json={"errors": []},
        traceparent=VALID,
    )
    async with unit_of_work(acme) as session:
        _, new = await JobRunRepository(session, acme).requeue(old)
    assert _stored(gen_world.conn, new.id) is None
    assert _stored(gen_world.conn, old) == VALID


@pytest.fixture
async def traced_api(
    gen_world: GenWorld,
) -> AsyncIterator[tuple[httpx.AsyncClient, InMemorySpanExporter]]:
    from app.core.observability import init_tracing, tracer_from
    from app.main import create_app

    exporter = InMemorySpanExporter()
    app = create_app()
    app.tracer = tracer_from(init_tracing(_settings(), exporter=exporter))  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, exporter


async def test_job_traceparent_api_enqueue_matches_server_span(
    gen_world: GenWorld, traced_api: tuple[httpx.AsyncClient, InMemorySpanExporter]
) -> None:
    from opentelemetry.trace import SpanKind

    client, exporter = traced_api
    response = await client.post(
        "/api/v1/listings/generate",
        headers=gen_world.headers("operator@acme.test"),
        json={"product_id": str(gen_world.product("acme", "KURTA-002")), "marketplace": "amazon"},
    )
    assert response.status_code == 202, response.text
    [server] = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    stored = _stored(gen_world.conn, uuid.UUID(response.json()["job_id"]))
    assert stored is not None
    assert stored.split("-")[1] == f"{server.context.trace_id:032x}"
