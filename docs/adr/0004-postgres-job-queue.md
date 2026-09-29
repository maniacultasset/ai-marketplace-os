# ADR-0004: Job queue in Postgres (procrastinate); drop Redis; psycopg 3 driver

- **Status:** accepted (user, in chat, 2026-09-27). **Gated on a 1-day spike** (below). If the
  spike fails, this ADR is superseded and the Canon's Redis + ARQ design stands unchanged.
- **Date:** 2026-09-27
- **Deciders:** the user (Tech Lead)
- **Evidence:** `docs/research/stack-2026-09.md`. Transactional enqueue was verified locally by the
  research skeptic.

## Context

The Canon uses Redis + ARQ (rows 39–42, 61, 75; ENV `REDIS_URL`, `ARQ_*`).
- **ARQ has been maintenance-only since 2025-10-18 (#510).** It pins `redis<6` and has unmerged bug
  fixes.
- **Redis (Memorystore Basic) isn't durable,** so the Canon already rebuilds most of a queue in
  Postgres:
  - `job_runs` as the source of truth;
  - commit-then-enqueue;
  - a 60 s sweeper for the gap between commit and enqueue;
  - a self-built dead-letter queue;
  - a log-line metric.
- For a bootstrapped client, Redis is also a second paid, managed service per environment, with
  its own network setup.

## Decision

1. **Queue:** procrastinate `>=3.10,<3.11`, using PostgreSQL 17 (ADR-0003) as the broker.
   - Two queues, `default` and `publish`, replace `q:default` and `q:publish`.
   - How many worker services to run is decided in ADR-0001.
2. **Transactional enqueue:** a job is deferred **inside the same transaction** as its domain rows
   and its `job_runs` row, through one tested helper. This removes the commit/enqueue gap and the
   sweeper.
3. **Driver:** the app's async engine moves from asyncpg to **psycopg 3** (`postgresql+psycopg`).
   This overturns the Addendum §3 "asyncpg only" freeze.
4. **`job_runs` stays the frontend contract and the source of truth.**
   - `arq_job_id` becomes `queue_job_id`.
   - Job payloads carry only the `job_run_id`. The worker loads `tenant_id` from `job_runs` and sets
     it with `set_config('app.tenant_id', …, true)`.
5. **Timeouts:** procrastinate has no per-task timeout (#1281). Every task is wrapped in
   `asyncio.timeout` (generation 300 s, publish 600 s, import 1800 s), and a timeout records
   `JOB_TIMEOUT` on a `failed` row.
6. **Retries:** procrastinate retry strategies with backoff for transport, 5xx and 429 errors.
   Generation uses `max_attempts=1` for domain failures, because the 3-attempt correction cap lives
   inside the graph (invariant 9).
7. **Dead letters:** unchanged Canon semantics, living in `job_runs`. A requeue inserts a new row
   with `parent_job_id`.
8. **Redis duties move to Postgres:**
   - **Rate limits:** an UNLOGGED fixed-window table (see ADR-0005).
   - **OAuth state:** a durable table, consumed once with `DELETE … RETURNING`.
   - **Refresh-token revocation** stays in Postgres (Canon row 61). The optional Redis fast path is
     dropped.
   - **Caches:** none in v1.
9. **Removed:**
   - Memorystore;
   - the env rows `REDIS_URL`, `ARQ_QUEUE_NAME`, `ARQ_MAX_JOBS` and `ARQ_JOB_TIMEOUT_SECONDS`;
   - the Redis compose service;
   - the sweeper cron;
   - the `dlq:{queue}` Redis counter.

## Spike (1 day, before S8 is planned; local PG17 first, then the staging DB)

The spike passes only if all of these hold:
1. **Transactional defer:** rollback leaves 0 rows in both tables; commit leaves 1 in each.
2. **Tenant context:** the job runs with the tenant context set, and RLS holds.
3. **Timeout:** a timeout produces a `failed` row with `JOB_TIMEOUT`.
4. **Retries:** retries back off and stop at the configured cap.
5. **Periodic tasks** run.
6. **Throughput:** a 1,000-row import plus 20 concurrent jobs, with p95 pickup latency under 2 s.
7. **Clean shutdown:** SIGTERM finishes or releases jobs within Cloud Run's 10 s window.
8. **Crash recovery:** after `kill -9` of a worker, stalled jobs are recovered.

## Consequences

- One service and one network hop fewer per environment, and no dual-write bug class.
- The Canon, Addendum, ENV and DATABASE.md rows that mention Redis or ARQ change. See the CANON.md
  amendments.
- **Trade-off:** future IAM database auth through the Cloud SQL connector supports async only on
  asyncpg. That feature was deferred anyway (Canon row 53).
- **Queue load now lands on Postgres.** Worker concurrency and pool sizes are budgeted in the
  restated connection ledger (ADR-0005).
