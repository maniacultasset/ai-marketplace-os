"""What a job body raises to steer the wrapper (DATABASE.md:1487 rule, PLAN Decision 5, Q1).

- `RetryableJobError(code)`: a transport failure, a 5xx or a 429. With attempts left the row goes
  back to `queued` and the wrapper raises `RetryLater`; at the cap it is `dead_letter` with
  `RETRY_EXHAUSTED`.
- `JobFailed(report)`: a domain failure, never retried; the row ends `failed` with `report`.
- `RetryLater(delay_s)`: raised by the wrapper only, after it has released the row; the queue's
  retry strategy re-runs the same queue entry after `delay_s`. `job_runs.attempt` is the only cap.
"""

from __future__ import annotations

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport


class RetryableJobError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class JobFailed(Exception):  # noqa: N818 - the name later steps import
    def __init__(self, report: ErrorReport) -> None:
        super().__init__(report.error_code.value if report.error_code else "failed")
        self.report = report


class RetryLater(Exception):  # noqa: N818 - a signal to the queue, not an error
    def __init__(self, delay_s: float) -> None:
        super().__init__(f"retry in {delay_s:.3f}s")
        self.delay_s = delay_s


def system_report(
    code: ErrorCode, message: str, *, attempt: int, max_attempts: int, request_id: str
) -> ErrorReport:
    """The one-item `ErrorReport` of a job-spine failure (`source=system`, field `request`):
    a timeout, retry exhaustion or an unexpected body error. `message` is our own text."""
    item = ErrorItem(
        field="request",
        error_code=code,
        message=message,
        source=ErrorSource.SYSTEM,
        retryable=False,
    )
    return ErrorReport(
        status="failed",
        error_code=code,
        retryable=False,
        attempts_used=attempt,
        max_attempts=max_attempts,
        errors=[item],
        request_id=request_id,
    )
