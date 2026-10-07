# ADR-0017: Job retry, shutdown release and lease recovery

- **Status:** proposed
- **Date:** 2026-10-07
- **Deciders:** Tech Lead (the user); recorded by the agent for S13a

## Context

ADR-0004 puts the queue in Postgres (procrastinate) with `job_runs` as the source of truth.
DATABASE.md:1487 proposes the outcome rule and asks for an ADR. The ADR-0004 spike (local half)
found three traps: procrastinate's `RetryStrategy(max_attempts=N)` counts retries, not runs; a
shutdown abort is re-queued only if the task's strategy accepts it, and it spends a
procrastinate attempt; and procrastinate has no reaper, so a `kill -9`ed worker leaves its job
`doing` forever. Generation has `max_attempts = 1`, so any attempt counted for an infrastructure
event would exhaust it.

## Decision

`job_runs.attempt` is the only attempt cap. The claim is the DATABASE.md:1027 compare-and-set
(`queued → running`, `attempt + 1`, a lease of the kind's timeout + 60 s), which also requires a
`trial` or `active` tenant; a queued job of a stopped tenant is cancelled. The wrapper decides
every outcome: an `Outcome` is `succeeded`/`partial`; the timeout is `failed` + `JOB_TIMEOUT`; a
domain failure (`JobFailed`) is `failed` and never retried; a transport, 5xx or 429 failure
(`RetryableJobError`) with attempts left goes back to `queued` and is retried after
`min(60, 2^(attempt-1))` seconds with jitter, and at the cap is `dead_letter` + `RETRY_EXHAUSTED`;
any other exception is `failed` + `INTERNAL_ERROR`. The queue's retry strategy has no cap: it
only turns the wrapper's `RetryLater(delay)` into a delayed retry and re-queues a shutdown abort
at once. A shutdown abort is not an attempt: the row goes back to `queued` with `attempt - 1`.
A reaper runs every minute: an expired `running` lease with attempts left goes back to `queued`
on a new queue entry (the dead entry is retired), exhausted is `dead_letter` + `JOB_TIMEOUT`; a
reaped attempt counts. Every write of a running attempt is fenced on `status = 'running' AND
attempt = <claimed>`, so a worker whose lease was reaped can never overwrite the newer attempt.
A staff requeue always inserts a new row with `parent_job_id` (CANON.md:61).

## Consequences

Deploys and scale-ins (SIGTERM, 8 s grace) never exhaust a job, generation included, and never
leave `job_runs` and the queue disagreeing. A crashed worker's job recovers after its lease (up to
about 31 minutes for a 1,800 s import); the spike's heartbeat-based stall detection (~30 s) was
not chosen because a lease bounds every kind the same way. Bodies must be idempotent (invariant
10): delivery is at-least-once, and the reaper re-runs a body. Later steps add per-kind terminal
and requeue hooks (`app/services/job_hooks.py`) rather than branching in the wrapper.
