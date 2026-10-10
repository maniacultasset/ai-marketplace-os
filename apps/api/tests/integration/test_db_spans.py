"""S23 T17 (AC9, AC10; D6): one CLIENT span per SQL statement under a recording parent, with
`db.system` and `db.operation.name` only: no statement text, no parameters."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
Conn = psycopg.Connection[tuple[Any, ...]]
CANARY = "Zq9canary"


def _settings(sampler: str) -> Any:
    from app.core.config import Settings

    return Settings(_env_file=None, app_env="local", otel_traces_sampler=sampler)


@pytest.fixture
def traced(auth_db: Conn) -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    from app.core.observability import init_tracing

    exporter = InMemorySpanExporter()
    yield init_tracing(_settings("always_on"), exporter=exporter), exporter


def _db(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("db ")]


async def _execute(*statements: str, params: dict[str, Any] | None = None) -> None:
    from app.db.session import get_engine
    from sqlalchemy import text

    async with get_engine().connect() as conn:
        for statement in statements:
            await conn.execute(text(statement), params or {})


async def test_db_spans_one_per_statement(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.db.session import get_engine
    from sqlalchemy import text

    provider, exporter = traced
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 0"))  # connect and pre-ping outside the parent
        with provider.get_tracer("t").start_as_current_span("parent") as parent:
            for n in range(3):
                await conn.execute(text(f"SELECT {n + 1}"))
    spans = _db(exporter)
    assert [s.name for s in spans] == ["db SELECT"] * 3
    for span in spans:
        assert span.kind is SpanKind.CLIENT
        assert span.parent is not None and span.parent.span_id == parent.get_span_context().span_id


async def test_db_spans_attributes_exact(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = traced
    with provider.get_tracer("t").start_as_current_span("parent"):
        await _execute("SELECT 1")
    for span in _db(exporter):
        assert dict(span.attributes or {}) == {
            "db.system": "postgresql",
            "db.operation.name": "SELECT",
        }


async def test_db_spans_no_sql_or_params(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = traced
    with provider.get_tracer("t").start_as_current_span("parent"):
        await _execute("SELECT CAST(:v AS text) AS secret_column", params={"v": CANARY})
    spans = _db(exporter)
    assert spans
    for span in spans:
        dumped = span.to_json()
        assert CANARY not in dumped and "secret_column" not in dumped


async def test_db_spans_none_without_recording_parent(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.core.observability import init_tracing

    _, exporter = traced
    await _execute("SELECT 1")  # no parent at all
    off = init_tracing(_settings("always_off"), exporter=exporter)
    with off.get_tracer("t").start_as_current_span("unsampled") as span:
        assert not span.is_recording()
        await _execute("SELECT 2")
    assert _db(exporter) == []


async def test_db_spans_error_status_on_failure(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.db.session import get_engine
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    provider, exporter = traced
    async with get_engine().connect() as conn:
        await conn.execute(text("CREATE TEMP TABLE s23_dup (k text PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO s23_dup VALUES (:k)"), {"k": CANARY})
        with (
            provider.get_tracer("t").start_as_current_span("parent"),
            pytest.raises(IntegrityError),
        ):
            await conn.execute(text("INSERT INTO s23_dup VALUES (:k)"), {"k": CANARY})
        await conn.rollback()
    [span] = _db(exporter)
    assert span.name == "db INSERT" and span.status.status_code is StatusCode.ERROR
    assert [(e.name, dict(e.attributes or {})) for e in span.events] == [
        ("exception", {"exception.type": "UniqueViolation"})
    ]
    assert CANARY not in span.to_json() and "DETAIL" not in span.to_json()


@pytest.mark.parametrize(
    ("statement", "operation"),
    [
        ("WITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
        ("select 1", "SELECT"),
        ("-- a comment\n/* another */ SELECT 1", "SELECT"),
        ("SET LOCAL statement_timeout = '5s'", "SET"),
    ],
    ids=["with_cte", "lowercase", "leading_comment", "set_local"],
)
async def test_db_spans_op_keyword_set(
    traced: tuple[TracerProvider, InMemorySpanExporter], statement: str, operation: str
) -> None:
    from app.db.session import get_engine
    from sqlalchemy import text

    provider, exporter = traced
    async with get_engine().connect() as conn, conn.begin():
        await conn.execute(text("SELECT 0"))
        with provider.get_tracer("t").start_as_current_span("parent"):
            await conn.execute(text(statement))
    assert [s.name for s in _db(exporter)] == [f"db {operation}"]


@pytest.mark.parametrize(
    ("statement", "operation"),
    [
        ("  \n\tupdate t set x = 1", "UPDATE"),
        ("/* multi\nline */ delete from t", "DELETE"),
        ("VACUUM", "OTHER"),
        ("", "OTHER"),
        ("COMMIT", "COMMIT"),
    ],
)
def test_operation_of(statement: str, operation: str) -> None:
    from app.db.tracing import operation_of

    assert operation_of(statement) == operation


async def test_db_spans_not_double_registered(
    traced: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    from app.db.session import get_engine
    from app.db.tracing import instrument_engine
    from sqlalchemy import text

    provider, exporter = traced
    instrument_engine(get_engine())
    instrument_engine(get_engine())
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 0"))
        with provider.get_tracer("t").start_as_current_span("parent"):
            await conn.execute(text("SELECT 1"))
    assert len(_db(exporter)) == 1
