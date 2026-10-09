"""The adapter call span (S23 AC7, AC10; C7): one CLIENT span per marketplace try, opened by the
adapter's HTTP client next to its `marketplace.call` log line (invariant 4: it lives in the
adapter).

`adapter_call_span(marketplace, operation, method, path_template)` yields a recorder; the client
calls `.status(code)` with the answer's status and `.error(error_code)` when it maps a failure.
The span's attributes are exactly `marketplace`, `adapter.operation`, `http.request.method`,
`url.template`, `http.response.status_code` (when answered), `duration_ms` and `error_code`
(when failed): never a URL, query, header, token, SKU or body (invariant 6). ERROR status for a
status >= 400 or a raised error; an exception is recorded by class name only. No span without a
recording parent (the job span or a traced request).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.core.tracer import get_tracer


class CallRecorder:
    def __init__(self, span: Any | None) -> None:
        self._span = span
        self.code: int | None = None
        self.error_code: str | None = None

    def status(self, code: int) -> None:
        self.code = code

    def error(self, error_code: str) -> None:
        self.error_code = str(error_code)


@contextmanager
def adapter_call_span(
    marketplace: str, operation: str, method: str, path_template: str
) -> Iterator[CallRecorder]:
    if not trace.get_current_span().is_recording():
        yield CallRecorder(None)
        return
    span = get_tracer().start_span(
        f"adapter {marketplace}.{operation}",
        kind=SpanKind.CLIENT,
        attributes={
            "marketplace": marketplace,
            "adapter.operation": operation,
            "http.request.method": method,
            "url.template": path_template,
        },
    )
    recorder = CallRecorder(span)
    started = time.perf_counter()
    failure: BaseException | None = None
    try:
        yield recorder
    except BaseException as exc:
        failure = exc
        raise
    finally:
        span.set_attribute("duration_ms", round((time.perf_counter() - started) * 1000, 1))
        if recorder.code is not None:
            span.set_attribute("http.response.status_code", recorder.code)
        code = recorder.error_code
        if failure is not None and code is None:
            report = getattr(failure, "report", None)
            reported = getattr(report, "error_code", None)
            code = None if reported is None else str(getattr(reported, "value", reported))
        if code is not None:
            span.set_attribute("error_code", code)
        if failure is not None:
            span.add_event("exception", {"exception.type": type(failure).__name__})
        if failure is not None or (recorder.code is not None and recorder.code >= 400):
            span.set_status(Status(StatusCode.ERROR))
        span.end()
