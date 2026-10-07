"""The queue's retry strategy (S13 Q1; spike REPORT §4 gotchas 1 and 3).

`job_runs.attempt` is the only attempt cap: the wrapper decides retry or `dead_letter` and, for a
retry, releases the row and raises `RetryLater(delay_s)`. This strategy only turns that into a
queue retry after the delay, and re-queues a shutdown abort (`CancelledError`/`JobAborted`) at
once, which the wrapper has already returned to `queued` uncounted. It has no cap of its own: a
cap here would count shutdown aborts, and after a few deploys the queue would stop retrying a job
`job_runs` still calls `queued`. Anything else gets no retry (the wrapper records every other
outcome itself and returns normally).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from procrastinate import BaseRetryStrategy, RetryDecision
from procrastinate.exceptions import JobAborted
from procrastinate.jobs import Job

from app.workers.errors import RetryLater


class JobRetryStrategy(BaseRetryStrategy):
    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        if isinstance(exception, RetryLater):
            return RetryDecision(
                retry_at=datetime.now(UTC) + timedelta(seconds=max(0.0, exception.delay_s))
            )
        if isinstance(exception, (asyncio.CancelledError, JobAborted)):
            return RetryDecision(retry_at=datetime.now(UTC))
        return None
