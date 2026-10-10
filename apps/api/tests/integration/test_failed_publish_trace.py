"""S23 T19 (AC7, AC10; Done-when 3): one forced marketplace failure is one trace, API request →
job → adapter call (with the DB spans of both), every span carrying the request's `request_id`;
no token, header, query string or body reaches any span.

S21 (publish) builds in parallel and is not on this base, so the job here is the `listing_generate`
job with its body replaced by a stand-in that sends the listing through the real Amazon client
to the fake marketplace with one forced attribute issue, and fails the job with the issue's
report. S21's hand-off swaps the stand-in for `POST /api/v1/listings/publish` and its job; the
chain under test (SERVER span → `job_runs.traceparent` → job span → adapter and DB spans) is
the same code.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .jobs_support import override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world", "override_body"]

REQUEST_ID = "s23-trace"
ISSUE = {
    "code": "90220",
    "message": "item_name is too long",
    "severity": "ERROR",
    "attributeNames": ["item_name"],
    "categories": ["INVALID_ATTRIBUTE"],
}
BODY = {"productType": "KURTA", "requirements": "LISTING_PRODUCT_ONLY", "attributes": {"item_name": [{"value": "Zq9bodycanary"}]}}  # fmt: skip


@pytest.fixture
async def traced(
    gen_world: GenWorld, override_body: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, InMemorySpanExporter, Any]]:
    """The app with an `always_on` in-memory exporter, and the generate job's body replaced by
    the stand-in publish (the fake marketplace forces one issue on SKU `S23-TRACE-1`)."""
    from app.core.config import Settings
    from app.core.observability import init_tracing, tracer_from
    from app.main import create_app
    from app.workers.errors import JobFailed, system_report
    from app.workers.registry import JobContext, Outcome
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import JobKind
    from tests.adapters.amazon_support import spapi_client
    from tests.support.fake_marketplace import build_fake

    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, app_env="local", otel_traces_sampler="always_on")
    app = create_app()
    app.tracer = tracer_from(init_tracing(settings, exporter=exporter))  # type: ignore[attr-defined]
    fake = build_fake()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(fake), base_url="http://fake.test"
    ) as control:
        await control.post("/__control", json={"spapi_issues": {"S23-TRACE-1": [ISSUE]}})

    async def stand_in_publish(ctx: JobContext) -> Outcome:
        client = spapi_client(fake)
        try:
            response = await client.put("S23-TRACE-1", BODY)
        finally:
            await client.aclose()
        if response.json.get("issues"):
            raise JobFailed(
                system_report(
                    ErrorCode.VALUE_NOT_ALLOWED,
                    "Amazon refused an attribute.",
                    attempt=ctx.attempt,
                    max_attempts=1,
                    request_id=ctx.request_id,
                )
            )
        return Outcome("succeeded")

    override_body(JobKind.LISTING_GENERATE, stand_in_publish)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, exporter, fake


async def _publish(client: httpx.AsyncClient, world: GenWorld, key: str) -> httpx.Response:
    headers = {**world.headers("operator@acme.test", key), "X-Request-ID": REQUEST_ID}
    return await client.post(
        "/api/v1/listings/generate",
        headers=headers,
        json={"product_id": str(world.product("acme", "KURTA-002")), "marketplace": "amazon"},
    )


def _by(spans: list[ReadableSpan], prefix: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name.startswith(prefix)]


async def test_failed_publish_single_trace(
    gen_world: GenWorld, traced: tuple[httpx.AsyncClient, InMemorySpanExporter, Any]
) -> None:
    from app.workers.wrapper import run_job

    client, exporter, _ = traced
    response = await _publish(client, gen_world, f"s23-trace-{uuid.uuid4()}")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    await run_job(job_id)
    row = gen_world.conn.execute(
        "SELECT status, error_code FROM job_runs WHERE id = %s", [job_id]
    ).fetchone()
    assert row == ("failed", "VALUE_NOT_ALLOWED")  # the stand-in's report, not a crash

    spans = list(exporter.get_finished_spans())
    [server] = [s for s in spans if s.kind is SpanKind.SERVER]
    [job] = _by(spans, "job ")
    adapter = _by(spans, "adapter ")
    db = _by(spans, "db ")
    assert adapter and db
    trace_ids = {s.context.trace_id for s in [server, job, *adapter, *db]}
    assert trace_ids == {server.context.trace_id}
    assert all((s.attributes or {}).get("request_id") == REQUEST_ID for s in spans)
    assert job.parent is not None and job.parent.span_id == server.context.span_id
    assert job.status.status_code is StatusCode.ERROR
    # SP-API answers an attribute issue with 200 and the issues in the body: the adapter span
    # records the answer; the job span carries the failure
    assert [(s.attributes or {}).get("http.response.status_code") for s in adapter] == [200]
    for span in adapter:
        assert span.parent is not None and span.parent.span_id == job.context.span_id


async def test_failed_publish_replay_same_job_and_trace(
    gen_world: GenWorld, traced: tuple[httpx.AsyncClient, InMemorySpanExporter, Any]
) -> None:
    """The same Idempotency-Key replays the same job: no second job row, no new job span."""
    client, exporter, _ = traced
    key = f"s23-replay-{uuid.uuid4()}"
    first = await _publish(client, gen_world, key)
    second = await _publish(client, gen_world, key)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert _by(list(exporter.get_finished_spans()), "job ") == []
    count = gen_world.conn.execute(
        "SELECT count(*) FROM job_runs WHERE id = %s", [first.json()["job_id"]]
    ).fetchone()
    assert count == (1,)


async def test_spans_never_leak(
    gen_world: GenWorld, traced: tuple[httpx.AsyncClient, InMemorySpanExporter, Any]
) -> None:
    from app.workers.wrapper import run_job
    from tests.support.fake_marketplace import CANARY

    client, exporter, _ = traced
    response = await _publish(client, gen_world, f"s23-leak-{uuid.uuid4()}")
    await run_job(response.json()["job_id"])
    dumped = "\n".join(s.to_json() for s in exporter.get_finished_spans())
    for secret in (
        CANARY,
        "Bearer",
        "Authorization",
        "x-amz-access-token",
        "Idempotency-Key",
        "marketplaceIds",
        "Zq9bodycanary",
        "KURTA-002",
        "S23-TRACE-1",
    ):
        assert secret not in dumped, secret
