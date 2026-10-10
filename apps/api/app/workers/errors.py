"""What a job body raises to steer the wrapper (DATABASE.md:1487 rule, PLAN Decision 5, Q1).

- `RetryableJobError(code)`: a transport failure, a 5xx or a 429. With attempts left the row goes
  back to `queued` and the wrapper raises `RetryLater`; at the cap it is `dead_letter` with
  `RETRY_EXHAUSTED`.
- `JobFailed(report, *, persist=None, zero_reservation=False)`: a domain failure, never retried;
  the row ends `failed` with `report`; `persist` runs in the same fenced terminal transaction
  (S18 T8: e.g. the failed run's `agent_runs`).
- `RetryLater(delay_s)`: raised by the wrapper only, after it has released the row; the queue's
  retry strategy re-runs the same queue entry after `delay_s`. `job_runs.attempt` is the only cap.
"""

from __future__ import annotations

from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.errors import ErrorItem, ErrorReport


class RetryableJobError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class JobFailed(Exception):  # noqa: N818 - the name later steps import
    def __init__(
        self, report: ErrorReport, *, persist: Any = None, zero_reservation: bool = False
    ) -> None:
        super().__init__(report.error_code.value if report.error_code else "failed")
        self.report = report
        self.persist = persist  # `registry.Persist | None` (typed there; no import cycle)
        self.zero_reservation = zero_reservation


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


# SQLSTATEs of a lost or killed backend: class 08 (connection exception) and 57P01-57P03
# (admin_shutdown, crash_shutdown, cannot_connect_now), e.g. `pg_terminate_backend` or a
# failover. Retrying such a body is safe for a kind whose policy retries (S26 T13; the publish
# job reads before it writes, imports are idempotent per row).
_TRANSIENT_SQLSTATES: Final = frozenset({"57P01", "57P02", "57P03"})


def is_transient_db_error(exc: BaseException) -> bool:
    """A database connection lost under the body (not a constraint, data or SQL error)."""
    import psycopg
    from sqlalchemy.exc import DBAPIError

    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    original = exc.orig if isinstance(exc, DBAPIError) else exc
    if not isinstance(original, psycopg.OperationalError):
        return False
    state = original.sqlstate
    return state is None or state.startswith("08") or state in _TRANSIENT_SQLSTATES
