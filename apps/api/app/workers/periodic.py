"""Periodic tasks (S13 PLAN D4, D12; DATABASE.md:952, :1028), registered on the queue app by the
worker. procrastinate's periodic deferrer runs in every worker, and `procrastinate_periodic_defers`
plus a `queueing_lock` per task make each schedule slot fire once across all workers. A periodic
queue entry carries only `{"timestamp"}`, never tenant data.

- `reaper` (every minute): `reap_once()`. It lists expired `running` rows through
  `platform_scope()` (the read-only cross-tenant session; repo rule R5 allows this module), then
  handles each job in its own tenant's `unit_of_work`: a stopped tenant's job is cancelled, one
  with attempts left is re-queued and re-deferred, an exhausted one is `dead_letter` +
  `JOB_TIMEOUT`; the dead queue entry is retired and the kind's terminal hook runs for the two
  terminal moves. A job whose reap raises rolls back alone, is logged `job.reap_failed` and
  skipped, and the pass goes on. Then stale worker rows are pruned (spike REPORT §4 gotcha 5).
- `prune_rate_limits` (every 5 min): counters whose window started over 5 minutes ago.
- `prune_idempotency_keys` (hourly): expired HTTP keys, tenant by tenant.
- `expire_trials` (hourly): `expire_trials()` (trials past their end → `suspended`).
- `retention_daily` (03:00): per tenant, in batches of 1,000, children first: `import_rows`,
  `imports`, then `job_runs` chains (`app.repositories.retention`); then the queue's own
  finished entries (succeeded after 7 days, every other final state after 90); then the image
  orphan sweep (S17b, below). A sweep failure is logged and never undoes the steps before it.
- `sweep_image_orphans` (inside `retention_daily`, S17b PLAN D9, CP11): per tenant, every object
  under `<tenant_id>/` older than 24 h whose key parses as that tenant's image key and has no
  `images` row (deleted rows count: replaced versions are kept) is deleted, one `list_objects_v2`
  page, one `registered_keys` query and one `delete_many` at a time, until the keys run out or the
  run budget is spent. Each run starts every tenant at a date-derived key and wraps, and visits
  tenants in a date-rotated order, so a run cut short reaches other ranges on later days. Logs
  carry counts and ids only.
- `refresh_marketplace_tokens` (every 5 min, S15): the `active` accounts whose token expires
  within 15 minutes, in `trial`/`active` tenants, oldest expiry first, at most 500 a pass, each
  through `refresh_account` (its own lock and short transactions), one after another. One
  account's failure is logged and the pass goes on.
- `prune_oauth_states` (hourly, S15): expired OAuth state and pending rows, tenant by tenant.
- `schedule_status_sync` (every minute, S21 T22; DATABASE.md:770): the due listings through
  `platform_scope()` (ids only), then per listing in its tenant's `unit_of_work`: re-check under
  `FOR UPDATE SKIP LOCKED`, enqueue a `listing_status_sync` job (no user) and set `next_sync_at`
  NULL in the same transaction, so a slow job is never enqueued twice and two workers never
  enqueue one slot twice. A listing that already has a live sync job is skipped.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import procrastinate
from mkos_schemas.codes import ErrorCode

from app.adapters.storage import get_storage, images_bucket
from app.core.alerts import (
    DEAD_LETTER_CAP,
    DEAD_LETTER_CAP_WINDOW,
    AlertKind,
    day_window,
    floor_window,
)
from app.core.config import get_settings
from app.core.flags import enabled_marketplaces
from app.core.logging import get_logger
from app.core.request_context import new_request_id, request_context
from app.db.session import platform_scope, unit_of_work
from app.repositories import rate_limits, retention
from app.repositories.idempotency import IdempotencyKeyRepository
from app.repositories.images import ImageRepository
from app.repositories.jobs import JobRunRepository, expired_running
from app.repositories.marketplace_accounts import due_accounts
from app.repositories.oauth_states import OAuthStateRepository, tenants_with_expired
from app.services.image_keys import parse_image_key
from app.services.job_hooks import run_terminal_hook
from app.workers import rollup
from app.workers.alerting import alert
from app.workers.errors import system_report
from app.workers.settings import STALLED_WORKER_S
from app.workers.token_refresh import REFRESH_MARGIN, refresh_account
from app.workers.wrapper import alert_dead_letter

log = get_logger(__name__)
REAPER_ID: Final = "reaper"
RATE_LIMIT_KEEP: Final = timedelta(minutes=5)


@dataclass
class ReapResult:
    requeued: list[UUID] = field(default_factory=list)
    dead_lettered: list[UUID] = field(default_factory=list)
    cancelled: list[UUID] = field(default_factory=list)
    skipped: list[UUID] = field(default_factory=list)
    pruned_workers: list[int] = field(default_factory=list)


def _timeout_report(job: Any, request_id: str) -> dict[str, Any]:
    report = system_report(
        ErrorCode.JOB_TIMEOUT,
        "the job's lease expired with no attempts left (its worker stopped)",
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        request_id=job.request_id or request_id,
    )
    return report.model_dump(mode="json")


async def reap_once(job_manager: Any | None = None) -> ReapResult:
    """One reaper pass. `job_manager` (the open queue app's) prunes stale worker rows."""
    result = ReapResult()
    with request_context(new_request_id()) as ctx:
        async with platform_scope() as session:
            expired = await expired_running(session)
        for job_id, tenant_id in expired:
            try:
                async with unit_of_work(tenant_id) as session:
                    reaped = await JobRunRepository(session, tenant_id).reap(
                        job_id, lambda job: _timeout_report(job, ctx.request_id)
                    )
                    if reaped.action in ("dead_letter", "cancelled") and reaped.job is not None:
                        await run_terminal_hook(session, reaped.job, None)
            except Exception as exc:
                # this job's transaction rolled back alone; it stays expired and is retried next
                # pass, and one bad row never blocks another tenant's recovery (S13 fix F1)
                log.exception(
                    "job.reap_failed",
                    job_run_id=str(job_id),
                    tenant_id=str(tenant_id),
                    error_class=type(exc).__name__,
                )
                result.skipped.append(job_id)
                continue
            if reaped.action == "dead_letter" and reaped.job is not None:
                # S23 T23: after the commit; JOB_TIMEOUT, deduped against the wrapper by job id
                await alert_dead_letter(tenant_id, job_id, reaped.job.kind, reaped.job.error_code)
            bucket = {
                "requeued": result.requeued,
                "dead_letter": result.dead_lettered,
                "cancelled": result.cancelled,
            }.get(reaped.action, result.skipped)
            bucket.append(job_id)
            log.info("job.reaped", job_run_id=str(job_id), action=reaped.action)
        if job_manager is not None:
            result.pruned_workers = list(await job_manager.prune_stalled_workers(STALLED_WORKER_S))
    return result


async def prune_rate_limits(now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    # S23 C3: alert dedupe/cap rows stay 25 h (a day-long dedupe window holds all day)
    pruned = await rate_limits.prune(
        current - RATE_LIMIT_KEEP, alert_before=current - rate_limits.ALERT_KEEP
    )
    log.info("periodic.rate_limits_pruned", rows=pruned)
    return pruned


async def prune_idempotency_keys() -> int:
    async with platform_scope() as session:
        tenants = await retention.tenant_ids(session)
    total = 0
    for tenant_id in tenants:
        async with unit_of_work(tenant_id) as session:
            total += await IdempotencyKeyRepository(session, tenant_id).prune_expired()
    log.info("periodic.idempotency_keys_pruned", rows=total)
    return total


async def run_expire_trials() -> int:
    expired = await retention.expire_trials()
    log.info("periodic.trials_expired", tenants=expired)
    return expired


REFRESH_BATCH: Final = 500


async def refresh_marketplace_tokens() -> dict[str, int]:
    """One refresh pass over the due accounts; the count per outcome."""
    async with platform_scope() as session:
        due = await due_accounts(
            session,
            margin=REFRESH_MARGIN,
            limit=REFRESH_BATCH,
            marketplaces=enabled_marketplaces(get_settings()),
        )
    counts: dict[str, int] = {}
    for tenant_id, account_id in due:
        try:
            outcome: str = await refresh_account(tenant_id, account_id)
        except Exception as exc:
            # this account's transactions rolled back alone; it is retried next pass, and one bad
            # row never blocks another tenant's refresh (the reaper's rule, S13 fix F1)
            log.exception(
                "marketplace.token_refresh_errored",
                tenant_id=str(tenant_id),
                account_id=str(account_id),
                error_class=type(exc).__name__,
            )
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    log.info("periodic.marketplace_tokens_refreshed", accounts=len(due), **counts)
    return counts


async def prune_oauth_states() -> int:
    async with platform_scope() as session:
        tenants = await tenants_with_expired(session)
    total = 0
    for tenant_id in tenants:
        async with unit_of_work(tenant_id) as session:
            total += await OAuthStateRepository(session, tenant_id).prune_expired()
    log.info("periodic.oauth_states_pruned", rows=total)
    return total


SYNC_BATCH: Final = 500


async def schedule_status_sync() -> int:
    """One scheduler pass (module docstring); the count of sync jobs enqueued."""
    from mkos_schemas.enums import JobKind

    from app.core.errors import ConflictError
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.listings import ListingRepository, due_for_sync
    from app.workers.payloads import StatusSyncPayload, dump

    async with platform_scope() as session:
        due = await due_for_sync(
            session, limit=SYNC_BATCH, marketplaces=enabled_marketplaces(get_settings())
        )
    enqueued = 0
    for tenant_id, listing_id in due:
        try:
            async with unit_of_work(tenant_id) as session:
                listings = ListingRepository(session, tenant_id)
                listing = await listings.lock_due_for_sync(listing_id)
                if listing is None:
                    continue
                await JobRunRepository(session, tenant_id).enqueue(
                    EnqueueSpec(
                        kind=JobKind.LISTING_STATUS_SYNC,
                        entity_type="listing",
                        entity_id=listing_id,
                        listing_id=listing_id,
                        created_by=None,
                        payload=dump(StatusSyncPayload(listing_id=listing_id, reason="scheduled")),
                    )
                )
                await listings.set_columns(listing_id, next_sync_at=None)
                enqueued += 1
        except ConflictError:  # a live sync job of the listing already exists
            log.info("sync.schedule_skipped_live_job", listing_id=str(listing_id))
        except Exception as exc:  # one listing's failure never blocks another tenant's sync
            log.exception(
                "sync.schedule_failed", listing_id=str(listing_id), error_class=type(exc).__name__
            )
    log.info("periodic.status_sync_scheduled", due=len(due), enqueued=enqueued)
    return enqueued


QUEUE_SUCCEEDED_HOURS: Final = 7 * 24
QUEUE_OTHER_HOURS: Final = 90 * 24


ORPHAN_AGE: Final = timedelta(hours=24)
SWEEP_RUN_BUDGET_S: Final = 300


@dataclass
class _Sweep:
    deadline: float
    deleted: int = 0
    pages: int = 0
    stopped: bool = False


def _sweep_start(tenant_id: UUID, run_date: Any) -> str:
    """The date-derived key a tenant's listing starts after (CP11): no stored resume state."""
    pick = random.Random(run_date.toordinal()).getrandbits(128)  # noqa: S311 - not a secret
    return f"{tenant_id}/{UUID(int=pick)}"


async def _sweep_range(
    sweep: _Sweep,
    tenant_id: UUID,
    cutoff: datetime,
    *,
    start_after: str | None,
    stop_after: str | None,
) -> None:
    storage = get_storage()
    prefix = f"{tenant_id}/"
    async for page in storage.iter_older_than(
        images_bucket(), prefix, cutoff, start_after=start_after
    ):
        sweep.pages += 1
        keys = []
        past_stop = False
        for info in page:
            if stop_after is not None and info.key > stop_after:
                past_stop = True
                break
            parsed = parse_image_key(info.key)
            if parsed is not None and parsed.tenant_id == tenant_id:
                keys.append(info.key)
        if keys:
            async with unit_of_work(tenant_id) as session:
                registered = await ImageRepository(session, tenant_id).registered_keys(keys)
            orphans = [k for k in keys if k not in registered]
            if orphans:
                await storage.delete_many(images_bucket(), orphans)
                sweep.deleted += len(orphans)
        if past_stop:
            return
        if time.monotonic() >= sweep.deadline:
            sweep.stopped = True
            last = parse_image_key(page[-1].key) if page else None
            log.info(
                "periodic.image_orphans_more_pending",
                tenant_id=str(tenant_id),
                product_id=None if last is None else str(last.product_id),
                image_id=None if last is None else str(last.image_id),
            )
            return


async def sweep_image_orphans(now: datetime | None = None) -> int:
    """The orphan sweep over every tenant's image prefix; returns the objects deleted."""
    now = now or datetime.now(UTC)
    async with platform_scope() as session:
        tenants = await retention.tenant_ids(session)
    if tenants:
        shift = now.date().toordinal() % len(tenants)
        tenants = tenants[shift:] + tenants[:shift]
    sweep = _Sweep(deadline=time.monotonic() + SWEEP_RUN_BUDGET_S)
    cutoff = now - ORPHAN_AGE
    for tenant_id in tenants:
        start = _sweep_start(tenant_id, now.date())
        await _sweep_range(sweep, tenant_id, cutoff, start_after=start, stop_after=None)
        if not sweep.stopped:
            await _sweep_range(sweep, tenant_id, cutoff, start_after=None, stop_after=start)
        if sweep.stopped:
            break
    log.info("periodic.image_orphans", deleted=sweep.deleted, pages=sweep.pages, more=sweep.stopped)
    return sweep.deleted


async def retention_daily(job_manager: Any | None = None) -> dict[str, int]:
    """The daily retention pass; returns the rows deleted per table across tenants (and the
    image orphans deleted)."""
    async with platform_scope() as session:
        tenants = await retention.tenant_ids(session)
    totals: dict[str, int] = {"import_rows": 0, "imports": 0, "job_runs": 0}
    for tenant_id in tenants:
        async with unit_of_work(tenant_id) as session:
            counts = await retention.prune_tenant(session, tenant_id)
        for table, (rows, _batches) in counts.items():
            totals[table] += rows
    if job_manager is not None:
        await job_manager.delete_old_jobs(nb_hours=QUEUE_SUCCEEDED_HOURS)
        await job_manager.delete_old_jobs(
            nb_hours=QUEUE_OTHER_HOURS,
            include_failed=True,
            include_cancelled=True,
            include_aborted=True,
        )
    try:
        totals["image_orphans"] = await sweep_image_orphans()
    except Exception as exc:
        # the object store is down or refused a delete: the row pruning above is committed and
        # stays; the sweep runs again tomorrow from another start key
        log.exception("periodic.image_orphans_failed", error_class=type(exc).__name__)
        totals["image_orphans"] = 0
    log.info("periodic.retention", **totals)
    return totals


# ---- S23 T24: the alert checks (AC11; D7, C8, C9) ----

ERROR_RATE_MIN_TERMINAL: Final = 20
ERROR_RATE_THRESHOLD: Final = Decimal("0.25")
QUEUE_LATENCY_S: Final = 600
BUDGET_THRESHOLDS: Final = (80, 100)
SLOT_WINDOW: Final = timedelta(minutes=30)
SUMMARY_LOOKBACK: Final = timedelta(hours=25)
CAP_PREFIX: Final = "alert:job_dead_lettered:cap:"


async def check_alerts(now: datetime | None = None) -> dict[str, int]:
    """Every 5 minutes (one slot across workers): the job error rate per kind (at least 20
    terminal jobs in 15 min and a failed share of 25 % or more), the LLM spend of today per tenant
    and in total (spend plus live reservations, as the kill switch counts) against 80 % and 100 %
    of its daily budget (a tenant's own `llm_daily_budget_usd`, else the default), the oldest
    queued job per queue (10 minutes or more), and one summary per closed over-cap dead-letter
    window. Each condition is deduped by its window; one failing emit never stops the others.
    Returns the events sent per kind."""
    current = now or datetime.now(UTC)
    async with rollup.rollup_session() as session:
        outcomes = await rollup.job_outcomes(session, current)
        spend = await rollup.spend_today(session, current)
        queues = await rollup.oldest_queued(session, current)
    settings = get_settings()
    slot = floor_window(current, SLOT_WINDOW)
    today = day_window(current)
    sent: dict[str, int] = {}

    async def emit(kind: AlertKind, **kwargs: Any) -> None:
        try:
            if await alert(kind, now=current, **kwargs):
                sent[kind] = sent.get(kind, 0) + 1
        except Exception as exc:  # one condition's failure never stops the others
            log.error("alert.check_failed", kind=kind, error_class=type(exc).__name__)

    for row in outcomes:
        if row.terminal < ERROR_RATE_MIN_TERMINAL:
            continue
        share = Decimal(row.failed) / Decimal(row.terminal)
        if share >= ERROR_RATE_THRESHOLD:
            await emit(
                "job_error_rate",
                key=row.kind,
                window_start=slot,
                job_kind=row.kind,
                count=row.failed,
                total=row.terminal,
                rate=share.quantize(Decimal("0.0001")),
            )
    # what the kill switch enforces (app/workers/budget.py::reserve): spend plus live
    # reservations against the tenant's own cap (else the default) and the platform cap
    global_limit = Decimal(str(settings.llm_daily_budget_usd))
    budgets: list[tuple[str, UUID | None, Decimal, Decimal, Decimal]] = [
        ("tenant", tenant, b.spent, b.used, b.cap) for tenant, b in sorted(spend.budgets.items())
    ]
    budgets.append(("global", None, spend.global_usd, spend.global_used, global_limit))
    for scope, tenant, usd, used, limit in budgets:
        for pct in BUDGET_THRESHOLDS:
            if limit <= 0 or used < limit * pct / 100:
                continue
            ids: dict[str, Any] = {} if tenant is None else {"tenant_id": tenant}
            await emit(
                "llm_budget",
                key=f"{scope}:{tenant or 'all'}:{pct}",
                window_start=today,
                scope=scope,
                threshold_pct=pct,
                spend_usd=usd,
                limit_usd=limit,
                **ids,
            )
    for queue in queues:
        if queue.oldest_s is not None and queue.oldest_s >= QUEUE_LATENCY_S:
            await emit(
                "queue_latency",
                key=queue.queue,
                window_start=slot,
                queue=queue.queue,
                oldest_s=queue.oldest_s,
            )
    cap_window = DEAD_LETTER_CAP_WINDOW
    closed = floor_window(current, cap_window)
    for bucket, window, n in await rate_limits.over_cap(
        CAP_PREFIX, DEAD_LETTER_CAP, closed_before=closed, since=current - SUMMARY_LOOKBACK
    ):
        job_kind = bucket.removeprefix(CAP_PREFIX)
        await emit(
            "job_dead_lettered",
            key=f"summary:{job_kind}",
            window_start=window,
            summary=True,
            job_kind=job_kind,
            count=n - DEAD_LETTER_CAP,
            total=n,
        )
    log.info("periodic.alerts_checked", **sent)
    return sent


async def _retention(context: procrastinate.JobContext, timestamp: int) -> None:
    with request_context(new_request_id()):
        await retention_daily(context.app.job_manager)


async def _reaper(context: procrastinate.JobContext, timestamp: int) -> None:
    await reap_once(context.app.job_manager)


def _plain(fn: Callable[[], Awaitable[Any]]) -> Callable[[int], Awaitable[None]]:
    async def task(timestamp: int) -> None:
        with request_context(new_request_id()):
            await fn()

    return task


# periodic_id -> (cron, the task function, pass_context); cron fields: minute hour dom month dow
PERIODIC: Final[dict[str, tuple[str, Any, bool]]] = {
    REAPER_ID: ("* * * * *", _reaper, True),
    "prune_rate_limits": ("*/5 * * * *", _plain(prune_rate_limits), False),
    "prune_idempotency_keys": ("0 * * * *", _plain(prune_idempotency_keys), False),
    "expire_trials": ("15 * * * *", _plain(run_expire_trials), False),
    "retention_daily": ("0 3 * * *", _retention, True),
    "marketplace_token_refresh": ("*/5 * * * *", _plain(refresh_marketplace_tokens), False),
    "oauth_states_prune": ("30 * * * *", _plain(prune_oauth_states), False),
    "schedule_status_sync": ("* * * * *", _plain(schedule_status_sync), False),
    "check_alerts": ("*/5 * * * *", _plain(check_alerts), False),
}


def register_periodic(app: procrastinate.App) -> None:
    """Register every periodic task on `app` (queue `default`, `queueing_lock` = its id, so a
    slot never queues twice); a second call is a no-op."""
    for periodic_id, (cron, fn, pass_context) in PERIODIC.items():
        name = f"periodic_{periodic_id}"
        if name in app.tasks:
            continue
        if pass_context:
            task: Any = app.task(name=name, queue="default", pass_context=True)(fn)
        else:
            task = app.task(name=name, queue="default", pass_context=False)(fn)
        app.periodic(cron=cron, periodic_id=periodic_id, queueing_lock=periodic_id)(task)
