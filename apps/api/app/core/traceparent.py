"""The W3C `traceparent` a job row carries (S23 AC8; PLAN D4): written from the span current at
enqueue time, read back by the worker as the job span's parent. Kept apart from
`app.core.observability` so `app.db` can stamp the column without importing Sentry or the
middleware.

- `current_traceparent()`: `00-<32 hex trace id>-<16 hex span id>-<01|00>` for the current valid
  span context (sampled or not), else None (no span: a periodic slot, a CLI requeue).
- `parse_traceparent(value)`: the remote parent context for a stored value, or None for NULL, a
  malformed value or all-zero ids: a bad value makes the job span a root, never an error.
"""

from __future__ import annotations

import re
from typing import Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT: Final = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")
_PROPAGATOR: Final = TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    flags = "01" if context.trace_flags.sampled else "00"
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags}"


def parse_traceparent(value: str | None) -> Context | None:
    if value is None or not TRACEPARENT.fullmatch(value):
        return None
    context = _PROPAGATOR.extract({"traceparent": value})
    if not trace.get_current_span(context).get_span_context().is_valid:
        return None  # all-zero trace or span id
    return context
