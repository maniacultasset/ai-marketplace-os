"""S23 T16 (AC7, AC8; C7): the wrapper runs each job inside one CONSUMER span `job <kind>`, a child
of the enqueuing request's span (`job_runs.traceparent`), with an exact attribute set and an ERROR
status for every outcome but `succeeded`. In-memory exporter, `always_on`."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from app.workers import wrapper
from app.workers.errors import JobFailed, RetryableJobError, RetryLater, system_report
from app.workers.registry import JobContext, Outcome
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from tests.support.otel_global import otel_global

from .conftest import LOCAL_HOSTS
from .jobs_support import JobWorld, insert_job, job_row, jobs_db, override_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["jobs_db", "otel_global", "override_body"]

VALID = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
ATTRIBUTES = {"job.id", "job.kind", "tenant_id", "job.attempt", "request_id"}


@pytest.fixture
def traced() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    from app.core.config import Settings
    from app.core.observability import init_tracing

    exporter = InMemorySpanExporter()
    settings = Settings(_env_file=None, app_env="local", otel_traces_sampler="always_on")
    yield init_tracing(settings, exporter=exporter), exporter


def _job_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("job ")]


async def _run(jobs_db: JobWorld, **columns: Any) -> uuid.UUID:
    job = insert_job(jobs_db.conn, jobs_db.tenant("acme"), **columns)
    with contextlib.suppress(RetryLater):
        await wrapper.run_job(str(job))
    return job


async def _ok(ctx: JobContext) -> Outcome:
    return Outcome("succeeded")


async def test_job_trace_child_of_request(
    jobs_db: JobWorld,
    override_body: Any,
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """Enqueued inside a request span: the job span's parent is that span, same trace id."""
    from app.repositories.jobs import EnqueueSpec, JobRunRepository
    from app.repositories.transactions import unit_of_work

    provider, exporter = traced
    override_body(JobKind.BULK_IMPORT, _ok)
    acme = jobs_db.tenant("acme")
    with provider.get_tracer("t").start_as_current_span("POST /x", kind=SpanKind.SERVER) as request:
        async with unit_of_work(acme) as session:
            made = await JobRunRepository(session, acme).enqueue(
                EnqueueSpec(
                    kind=JobKind.BULK_IMPORT,
                    entity_type="import",
                    entity_id=uuid.uuid4(),
                    created_by=None,
                    payload={"import_id": str(uuid.uuid4())},
                )
            )
    await wrapper.run_job(str(made.job.id))
    [span] = _job_spans(exporter)
    assert span.kind is SpanKind.CONSUMER and span.name == "job bulk_import"
    assert span.parent is not None
    assert span.parent.span_id == request.get_span_context().span_id
    assert span.context.trace_id == request.get_span_context().trace_id


async def test_job_trace_null_is_root(
    jobs_db: JobWorld, override_body: Any, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    _, exporter = traced
    override_body(JobKind.BULK_IMPORT, _ok)
    await _run(jobs_db)
    [span] = _job_spans(exporter)
    assert span.parent is None


@pytest.mark.parametrize(
    "value",
    [
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
    ],
    ids=["all_zero_trace", "all_zero_span"],
)
async def test_job_trace_malformed_is_root(
    jobs_db: JobWorld,
    override_body: Any,
    traced: tuple[TracerProvider, InMemorySpanExporter],
    value: str,
) -> None:
    _, exporter = traced
    override_body(JobKind.BULK_IMPORT, _ok)
    job = await _run(jobs_db, traceparent=value)
    [span] = _job_spans(exporter)
    assert span.parent is None
    assert job_row(jobs_db.conn, job)["status"] == "succeeded"


def _report() -> Any:
    return system_report(
        ErrorCode.INTERNAL_ERROR, "x", attempt=1, max_attempts=3, request_id="rid-x"
    )


async def _partial(ctx: JobContext) -> Outcome:
    return Outcome("partial", error=_report())


async def _failed(ctx: JobContext) -> Outcome:
    raise JobFailed(_report())


async def _timeout(ctx: JobContext) -> Outcome:
    raise TimeoutError


async def _retryable(ctx: JobContext) -> Outcome:
    raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)


BODIES = {
    "succeeded": (_ok, StatusCode.UNSET),
    "partial": (_partial, StatusCode.ERROR),
    "failed": (_failed, StatusCode.ERROR),
    "timeout": (_timeout, StatusCode.ERROR),
    "retryable": (_retryable, StatusCode.ERROR),
}


@pytest.mark.parametrize("case", sorted(BODIES))
async def test_job_trace_status(
    jobs_db: JobWorld,
    override_body: Any,
    traced: tuple[TracerProvider, InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from app.workers.policy import POLICIES, JobPolicy

    _, exporter = traced
    body, want = BODIES[case]
    if case == "timeout":
        p = POLICIES[JobKind.BULK_IMPORT]

        async def slow(ctx: JobContext) -> Outcome:
            import asyncio

            await asyncio.sleep(5)
            return Outcome("succeeded")

        monkeypatch.setattr(
            wrapper,
            "POLICIES",
            {
                **POLICIES,
                JobKind.BULK_IMPORT: JobPolicy(
                    p.kind, p.queue, 0, p.max_attempts, p.retry_transport
                ),
            },
        )
        body = slow
    override_body(JobKind.BULK_IMPORT, body)
    await _run(jobs_db)
    [span] = _job_spans(exporter)
    assert span.status.status_code is want, case


async def test_job_trace_reaper_redefer_keeps_traceparent(
    jobs_db: JobWorld, override_body: Any, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    """The row's traceparent survives a re-delivery: attempt 2's span has the same parent."""
    _, exporter = traced
    override_body(JobKind.BULK_IMPORT, _retryable)
    job = await _run(jobs_db, traceparent=VALID)
    assert job_row(jobs_db.conn, job)["status"] == "queued"
    override_body(JobKind.BULK_IMPORT, _ok)
    await wrapper.run_job(str(job))
    first, second = _job_spans(exporter)
    assert job_row(jobs_db.conn, job)["traceparent"] == VALID
    assert first.parent is not None and second.parent is not None
    assert first.parent.span_id == second.parent.span_id == int(VALID.split("-")[2], 16)
    assert (first.attributes or {})["job.attempt"] == 1
    assert (second.attributes or {})["job.attempt"] == 2


async def test_job_trace_retry_fields_own_trace(
    jobs_db: JobWorld, override_body: Any, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    """A follow-up job enqueued by a later request (S21's retry-fields shape: a new row with
    `parent_job_id`) joins that request's trace, not the first job's."""
    other = "00-11111111111111111111111111111111-2222222222222222-01"
    _, exporter = traced
    override_body(JobKind.BULK_IMPORT, _ok)
    first = await _run(jobs_db, traceparent=VALID)
    await _run(jobs_db, traceparent=other, parent_job_id=first)
    one, two = _job_spans(exporter)
    assert one.context.trace_id == int(VALID.split("-")[1], 16)
    assert two.context.trace_id == int(other.split("-")[1], 16)


async def test_job_trace_attributes_exact(
    jobs_db: JobWorld, override_body: Any, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    _, exporter = traced
    override_body(JobKind.BULK_IMPORT, _ok)
    job = await _run(jobs_db, request_id="rid-s23-job")
    [span] = _job_spans(exporter)
    attrs = dict(span.attributes or {})
    assert set(attrs) == ATTRIBUTES
    assert attrs["job.id"] == str(job) and attrs["job.kind"] == "bulk_import"
    assert attrs["tenant_id"] == str(jobs_db.tenant("acme"))
    assert (attrs["job.attempt"], attrs["request_id"]) == (1, "rid-s23-job")


async def test_job_trace_exception_class_only(
    jobs_db: JobWorld, override_body: Any, traced: tuple[TracerProvider, InMemorySpanExporter]
) -> None:
    _, exporter = traced

    async def boom(ctx: JobContext) -> Outcome:
        raise ValueError("Zq9canary secret text")

    override_body(JobKind.BULK_IMPORT, boom)
    await _run(jobs_db)
    [span] = _job_spans(exporter)
    events = [(e.name, dict(e.attributes or {})) for e in span.events]
    assert events == [("exception", {"exception.type": "ValueError"})]
    assert "Zq9canary" not in repr(span.to_json())


async def test_global_api_span_nests_under_job(
    otel_global: None,
    jobs_db: JobWorld,
    override_body: Any,
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    """Under the worker's global provider, a task body's `trace.get_tracer()` span is a child of
    the job span (PLAN AC7)."""
    provider, exporter = traced
    trace.set_tracer_provider(provider)

    async def body(ctx: JobContext) -> Outcome:
        with trace.get_tracer("x").start_as_current_span("node"):
            pass
        return Outcome("succeeded")

    override_body(JobKind.BULK_IMPORT, body)
    await _run(jobs_db)
    [job] = _job_spans(exporter)
    [node] = [s for s in exporter.get_finished_spans() if s.name == "node"]
    assert node.parent is not None and node.parent.span_id == job.context.span_id
    assert node.context.trace_id == job.context.trace_id
