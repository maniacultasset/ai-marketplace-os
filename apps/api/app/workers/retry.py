"""The queue's retry strategy (S13 Q1; spike REPORT §4 gotchas 1 and 3).

`job_runs.attempt` is the only attempt cap for the job body: the wrapper decides retry or
`dead_letter` and, for a retry, releases the row and raises `RetryLater(delay_s)`. This strategy
turns that into a queue retry after the delay, and re-queues a shutdown abort
(`CancelledError`/`JobAborted`) at once, which the wrapper has already returned to `queued`
uncounted. Neither has a cap here: a cap would count shutdown aborts, and after a few deploys the
queue would stop retrying a job `job_runs` still calls `queued`.

Any other `Exception` escaped the wrapper outside the body (it records every body outcome
itself): an infrastructure error while naming, claiming or finishing the job (a DB blip, a
failover, a pool timeout). It is retried with backoff up to `INFRA_RETRY_CAP` queue attempts
(procrastinate's own counter, shutdowns included; S13 fix F2). Past the cap the queue finishes
the entry `failed` while the row is still `queued`; the reaper then ends the row
(`dead_letter` + `INTERNAL_ERROR`, audit JOBS-2), so it is never left `queued` with a dead entry.
Redelivery is safe in every case: a claim that never committed leaves the row
`queued` and the next delivery claims it; one that committed leaves it `running`, the claim CAS
then fails and the delivery is dropped, and the reaper recovers the row at lease expiry.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final

from procrastinate import BaseRetryStrategy, RetryDecision
from procrastinate.exceptions import JobAborted
from procrastinate.jobs import Job

from app.workers.errors import RetryLater
from app.workers.policy import backoff_s

INFRA_RETRY_CAP: Final = 20
_rand = secrets.SystemRandom()


class JobRetryStrategy(BaseRetryStrategy):
    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        if isinstance(exception, RetryLater):
            return RetryDecision(
                retry_at=datetime.now(UTC) + timedelta(seconds=max(0.0, exception.delay_s))
            )
        if isinstance(exception, (asyncio.CancelledError, JobAborted)):
            return RetryDecision(retry_at=datetime.now(UTC))
        if isinstance(exception, Exception) and job.attempts < INFRA_RETRY_CAP:
            delay = backoff_s(job.attempts + 1, _rand.random())
            return RetryDecision(retry_at=datetime.now(UTC) + timedelta(seconds=delay))
        return None
