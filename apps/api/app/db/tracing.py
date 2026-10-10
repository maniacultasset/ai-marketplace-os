"""DB query spans (S23 AC9; PLAN D6; R12761b): one CLIENT span `db <OPERATION>` per statement,
hand-rolled on SQLAlchemy's cursor events (no `opentelemetry-instrumentation-sqlalchemy`).

- A span starts only under a recording parent (a sampled request or job span), so an untraced
  request or a background loop costs nothing and never makes a root span.
- Attributes are exactly `db.system = postgresql` and `db.operation.name`: the statement's first
  SQL keyword from a fixed set (`SELECT|INSERT|UPDATE|DELETE|WITH|SET|BEGIN|COMMIT|ROLLBACK`,
  anything else `OTHER`). The statement text and its parameters are never put on a span.
- A failing statement ends its span with ERROR status and an `exception` event naming the
  exception class only (a driver message can quote values).
"""

from __future__ import annotations

import re
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.tracer import get_tracer

OPERATIONS: Final = frozenset(
    {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "SET", "BEGIN", "COMMIT", "ROLLBACK"}
)
_LEADING: Final = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.S)
_WORD: Final = re.compile(r"[A-Za-z]+")
_SPAN_ATTR: Final = "_mkos_span"


def operation_of(statement: str) -> str:
    """The statement's first keyword, upper-cased, from `OPERATIONS`; else `OTHER`."""
    rest = _LEADING.sub("", statement, count=1)
    word = _WORD.match(rest)
    name = word.group(0).upper() if word else ""
    return name if name in OPERATIONS else "OTHER"


def _before(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    if context is None or not trace.get_current_span().is_recording():
        return
    operation = operation_of(statement)
    span = get_tracer().start_span(
        f"db {operation}",
        kind=SpanKind.CLIENT,
        attributes={"db.system": "postgresql", "db.operation.name": operation},
    )
    setattr(context, _SPAN_ATTR, span)


def _after(
    conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
) -> None:
    span = getattr(context, _SPAN_ATTR, None)
    if span is not None:
        setattr(context, _SPAN_ATTR, None)
        span.end()


def _error(exception_context: Any) -> None:
    context = exception_context.execution_context
    span = getattr(context, _SPAN_ATTR, None) if context is not None else None
    if span is None:
        return
    setattr(context, _SPAN_ATTR, None)
    error = exception_context.original_exception
    span.add_event("exception", {"exception.type": type(error).__name__})
    span.set_status(Status(StatusCode.ERROR))
    span.end()


def instrument_engine(engine: AsyncEngine) -> None:
    """Attach the three listeners to `engine` once (a second call is a no-op)."""
    sync = engine.sync_engine
    if event.contains(sync, "before_cursor_execute", _before):
        return
    event.listen(sync, "before_cursor_execute", _before)
    event.listen(sync, "after_cursor_execute", _after)
    event.listen(sync, "handle_error", _error)
