"""The marketplace's dry run before approval (SI11 T7; ADR-0028 proposed; P:A1-A2, D4-D6;
CP-D5, CP-D16, CP-D23, CP-D24, CP-D26).

The dry run is a second body of the `listing_status_sync` kind (`op = "preview"`, P:D4): `JobKind`
is closed, and the sync kind already runs on the publish queue with one live job per listing. It
asks Amazon (`VALIDATION_PREVIEW`, which saves nothing) about the listing's current version and
writes Amazon's issues as `amazon_adapter` rows of the `preview` channel (CP-D4).

- `request_preview(uow, listing, version_id, trigger=...)` enqueues one only when the marketplace
  is Amazon, the setting is on, the listing is not paused or archived, its account is `active`
  and the account breaker is closed, and the status is `validated`, `needs_review` or `approved`
  (a `resolution` re-check after a repair may also run from `blocked/validation_errors`). The job
  key is `sha256("preview:" listing ":" version ":" repair_set ":" facts)` (CP-D26; fix round 1):
  the same version with the same repairs and the same seller facts (`listing_facts_digest` of the
  product family: a preview sends the origin, barcode and identity facts, which change without a
  new version) is the same payload, so a second verdict is a replay. A replay never leaves the
  check `pending`: it takes the earlier job's verdict (`pending` only while that job is live),
  unless that verdict contradicts the version's open pre-check ERROR rows (fix round 2: facts
  A, then B, then A again replay A's 'clean' while B's ERROR is open): then a fresh job is
  keyed on those rows too, and its verdict closes or keeps them. A live sync job of the
  listing means no insert: the check is marked `pending` and the live job's terminal enqueues it
  (CP-D5). The insert runs in a SAVEPOINT, so the live-index race never fails the caller's
  request or terminal transaction. Returns the new job's id, or None.
- `apply_preview(...)` is the job's `persist` (under the listing lock). It writes only while the
  version is still current and the listing is still pre-approval (or `blocked/validation_errors`);
  otherwise the verdict is stale and nothing is written, so a late ERROR never lands on an
  approved or published version (the publish gate would refuse it). Then: the issues (full scope
  of the sent paths), `remote_check_*`, and the move: an ERROR moves `validated`, `needs_review`
  or `approved` to `blocked/validation_errors` (`revalidate`; an approval is voided); a clean
  verdict on a `blocked/validation_errors` listing with no other open ERROR moves it back to the
  Rules verdict (`validated`, or `needs_review` while a WARNING row is open). `failed` moves
  nothing.
- `on_terminal(session, job, outcome)`: a failed or dead-lettered preview marks the check
  `failed` (never a poll, CP-D16); an applied-looking reply opens the account breaker's task.
- `enqueue_pending(uow, listing_id)` runs after every terminal of the kind: a check left
  `pending` behind a live job gets its own job now.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Final, Literal

from mkos_schemas.enums import JobKind, Severity
from mkos_schemas.errors import ErrorReport

from app.adapters.base import RemotePreview
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.core.resolved_facts import listing_facts_digest
from app.db.models.ops import JobRun
from app.repositories.jobs import Enqueued, EnqueueSpec
from app.repositories.listings import JobActor
from app.repositories.repairs import repair_set_digest
from app.repositories.resolution import BREAKER_KIND, BREAKER_TARGET
from app.repositories.uow import TenantUnitOfWork, repositories_on
from app.services.issues import adapter_source, sync_issues
from app.workers.payloads import StatusSyncPayload, dump
from app.workers.registry import Outcome

log = get_logger(__name__)

Trigger = Literal["system", "resolution"]
PRE_APPROVAL: Final = frozenset({"validated", "needs_review", "approved"})
ENDED: Final = frozenset({"failed", "dead_letter", "cancelled"})
STALE: Final = {"skipped": "stale"}
BREAKER_QUESTION: Final = (
    "Check this Amazon account: its pre-check answered as if it saved a listing."
)
BREAKER_REASON: Final = (
    "preview_applied: Amazon's dry run (VALIDATION_PREVIEW) answered with a status other than "
    "VALID or INVALID, so Kuinn stopped the pre-checks on this account until staff look at it."
)


def is_preview(job: JobRun) -> bool:
    return isinstance(job.payload_json, dict) and job.payload_json.get("op") == "preview"


def previewable(listing: Any, trigger: Trigger, *, recheck: bool = False) -> bool:
    """The statuses a dry run may start from (and still write in, `apply_preview`). `recheck`
    (fix round 2): a validate whose current version holds an open pre-check ERROR may re-ask
    from `blocked/validation_errors`, as a resolution re-check does."""
    if listing.paused_at is not None or listing.archived_at is not None:
        return False
    if listing.status in PRE_APPROVAL:
        return True
    return (
        (trigger == "resolution" or recheck)
        and listing.status == "blocked"
        and listing.status_reason == "validation_errors"
    )


def preview_key(
    listing_id: uuid.UUID, version_id: uuid.UUID, repair_set: bytes, facts: bytes = b""
) -> str:
    raw = f"preview:{listing_id}:{version_id}:{repair_set.hex()}:{facts.hex()}"
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def after_key(basis: str, latest_id: uuid.UUID) -> str:
    """The key of a fresh job for inputs `basis` asked after the version's newest preview
    `latest_id` (fix round 3): two requests after the same newest job enqueue one."""
    raw = f"preview-after:{basis}:{latest_id}"
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _basis_of(job: JobRun) -> str | None:
    """The inputs key a preview judged: its payload's `basis`, or its own key (before round 3)."""
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    basis = payload.get("basis")
    return basis if isinstance(basis, str) else job.idempotency_key


def replay_disagrees(verdict: str, open_keys: list[str]) -> bool:
    """A replayed verdict that contradicts the version's open pre-check rows (fix round 2): a
    'clean' replay while an ERROR of this version is open (a later verdict on other facts opened
    it), or an 'issues' replay while none is open. Such a replay is not taken."""
    return (verdict == "clean" and bool(open_keys)) or (verdict == "issues" and not open_keys)


def replayed_check(job: JobRun) -> str:
    """The check state a replayed key leaves: the earlier job's verdict, `pending` while it is
    still live, `failed` when it ended without one (stale, failed or dead-lettered)."""
    if job.status in ("queued", "running"):
        return "pending"
    result = job.result_json if isinstance(job.result_json, dict) else {}
    verdict = result.get("remote_check")
    return verdict if verdict in ("clean", "issues", "failed") else "failed"


async def request_preview(
    uow: TenantUnitOfWork,
    listing: Any,
    version_id: uuid.UUID | None,
    *,
    trigger: Trigger = "system",
    recheck: bool = False,
) -> uuid.UUID | None:
    settings = get_settings()
    if (
        version_id is None
        or listing.marketplace != "amazon"
        or not settings.amazon_validation_preview_enabled
        or not previewable(listing, trigger, recheck=recheck)
    ):
        return None
    # the account a publish would use: the listing's own once set, else the tenant's active one
    account = await uow.marketplace_accounts.active_for_publish(
        listing.marketplace, listing.marketplace_account_id
    )
    if account is None:
        return None
    if await uow.resolution.preview_breaker_open(account):
        log.info("preview.breaker_open", listing_id=str(listing.id))
        return None
    if await uow.job_runs.live_for_listing(listing.id, JobKind.LISTING_STATUS_SYNC) is not None:
        await uow.listings.set_remote_check(listing.id, version_id, "pending")
        return None
    repairs = await uow.listing_repairs.live_for_listing(listing.id)
    family = await uow.product_facts.resolve_family(listing.product_id)
    facts = b"" if family is None else listing_facts_digest(*family)
    basis = preview_key(listing.id, version_id, repair_set_digest(repairs), facts)
    latest = await uow.job_runs.latest_preview(listing.id, version_id)
    if latest is not None and _basis_of(latest) == basis:
        verdict = replayed_check(latest)
        open_keys = await _open_preview_keys(uow, listing.id, version_id)
        if verdict in ("clean", "issues") and not replay_disagrees(verdict, open_keys):
            # the newest verdict judged these very inputs and matches the open rows: a replay
            await uow.listings.set_remote_check(listing.id, version_id, verdict)
            return None
    # fix round 3: first ask, or the inputs changed since the newest verdict (A -> B -> A -> B),
    # or that verdict did not hold: a fresh job, keyed after the newest one so a double request
    # still enqueues one
    key = basis if latest is None else after_key(basis, latest.id)
    enqueued = await _enqueue(uow, listing, version_id, trigger, key, basis)
    if enqueued is None:
        return None
    if not enqueued.created:  # a concurrent request enqueued this very job
        await uow.listings.set_remote_check(listing.id, version_id, replayed_check(enqueued.job))
        return None
    await uow.listings.set_remote_check(listing.id, version_id, "pending")
    log.info(
        "preview.requested",
        listing_id=str(listing.id),
        job_run_id=str(enqueued.job.id),
        trigger=trigger,
    )
    return enqueued.job.id


async def _enqueue(
    uow: TenantUnitOfWork,
    listing: Any,
    version_id: uuid.UUID,
    trigger: Trigger,
    key: str,
    basis: str,
) -> Enqueued | None:
    """The preview job insert in a SAVEPOINT; None when a sync job went live meanwhile (the
    check is left `pending`, and that job's terminal enqueues this one)."""
    spec = EnqueueSpec(
        kind=JobKind.LISTING_STATUS_SYNC,
        entity_type="listing",
        entity_id=listing.id,
        listing_id=listing.id,
        created_by=None,
        job_key=key,
        payload=dump(
            StatusSyncPayload(
                listing_id=listing.id,
                op="preview",
                version_id=version_id,
                trigger=trigger,
                basis=basis,
            )
        ),
    )
    try:
        async with uow.session.begin_nested():
            return await uow.job_runs.enqueue(spec)
    except ConflictError:
        await uow.listings.set_remote_check(listing.id, version_id, "pending")
        return None


async def _open_preview_keys(
    uow: TenantUnitOfWork, listing_id: uuid.UUID, version_id: uuid.UUID
) -> list[str]:
    rows = await uow.listing_issues.open_errors(listing_id, ["amazon_adapter"], channel="preview")
    return [row.dedupe_key for row in rows if row.listing_version_id == version_id]


async def apply_preview(
    uow: TenantUnitOfWork,
    job_id: uuid.UUID,
    listing_id: uuid.UUID,
    version_id: uuid.UUID,
    result: RemotePreview,
    trigger: Trigger,
) -> dict[str, Any]:
    """The verdict's writes (module docstring); returns what the job's result records."""
    listing = await uow.listings.get_for_update(listing_id)
    if (
        listing is None
        or listing.current_version_id != version_id
        or not previewable(listing, "resolution")
    ):
        log.info("preview.stale", job_run_id=str(job_id), listing_id=str(listing_id))
        return dict(STALE)
    if result.status == "failed":
        await uow.listings.set_remote_check(
            listing.id, version_id, "failed", only_version=version_id
        )
        return {"remote_check": "failed"}
    errors = list(result.errors)
    report = ErrorReport(status="failed" if errors else "passed", errors=errors)
    await sync_issues(
        uow,
        listing,
        adapter_source(listing.marketplace),
        report,
        run_id=None,
        cause="revalidate",
        scope_fields=sorted({*result.scope, "listing", *(e.field for e in errors)}),
        critical_keys=await uow.product_facts.critical_keys(),
        version_id=version_id,
        job_id=job_id,
        channel="preview",
    )
    blocking = any(e.severity is Severity.ERROR for e in errors)
    status = "issues" if blocking else "clean"
    await uow.listings.set_remote_check(listing.id, version_id, status, only_version=version_id)
    actor = JobActor(job_id)
    if blocking and listing.status in PRE_APPROVAL:
        await uow.listings.move(
            listing,
            to="blocked",
            event="revalidate",
            reason="validation_errors",
            actor=actor,
            clear_approval=listing.status == "approved",
        )
    elif (
        not blocking
        and listing.status == "blocked"
        and listing.status_reason == "validation_errors"
    ):
        severities = await uow.resolution.open_severities(listing.id)
        if "error" not in severities:
            await uow.listings.move(
                listing,
                to="needs_review" if "warning" in severities else "validated",
                event="revalidate",
                actor=actor,
            )
    log.info(
        "preview.applied_verdict",
        job_run_id=str(job_id),
        listing_id=str(listing.id),
        remote_check=status,
        items=len(errors),
        trigger=trigger,
    )
    return {"remote_check": status}


async def open_breaker(uow: TenantUnitOfWork, account_id: uuid.UUID) -> uuid.UUID | None:
    """CP-D24: one `marketplace_error_review` account task; the pre-checks of the account stop
    until it is closed (SI9 `tasks_close`)."""
    task_id = await uow.human_tasks.open_account_task(
        BREAKER_KIND,
        account_id,
        BREAKER_TARGET,
        BREAKER_QUESTION,
        BREAKER_REASON,
        {"kind": "acknowledge"},
    )
    log.error("preview.breaker_opened", account_id=str(account_id))
    return task_id


async def on_terminal(session: Any, job: JobRun, outcome: Outcome | None) -> None:
    """The preview's part of the `listing_status_sync` terminal hook (module docstring)."""
    del outcome
    if job.listing_id is None:
        return
    uow = repositories_on(session, job.tenant_id)
    payload = StatusSyncPayload.model_validate(job.payload_json or {})
    listing = await uow.listings.get(job.listing_id)
    if listing is None:
        return
    if job.status in ENDED and payload.version_id is not None:
        if listing.remote_check_version_id == payload.version_id:
            await uow.listings.set_remote_check(
                listing.id, payload.version_id, "failed", only_version=payload.version_id
            )
        return
    result = job.result_json if isinstance(job.result_json, dict) else {}
    if result.get("preview_applied") is True and result.get("account_id"):
        await open_breaker(uow, uuid.UUID(str(result["account_id"])))
        return
    if result.get("remote_check") == "issues":
        from app.services.resolution.run import resolve  # T12: the pre-approval entry

        fresh = await uow.listings.get(listing.id)
        await resolve(uow, fresh, entry="preview", job=job)


async def enqueue_pending(session: Any, job: JobRun) -> None:
    """After every terminal of the kind: a check left `pending` (it waited behind a live job)
    gets its own job for the listing's current version."""
    if job.listing_id is None:
        return
    uow = repositories_on(session, job.tenant_id)
    listing = await uow.listings.get(job.listing_id)
    if listing is None or listing.remote_check_status != "pending":
        return
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    trigger: Trigger = "resolution" if payload.get("trigger") == "resolution" else "system"
    if await request_preview(uow, listing, listing.current_version_id, trigger=trigger) is None:
        await settle_orphan_check(uow, await uow.listings.get(listing.id))


async def settle_orphan_check(uow: TenantUnitOfWork, listing: Any) -> None:
    """Part B fix round 2: a `pending` stamp with no live `listing_status_sync` job behind it
    settles `failed` (the dry run ended without a verdict). It is left when the job it waited
    behind ended while the listing could not be previewed (a fix job's `generating`, then
    `failed`): no job would ever give it a verdict, and the read would wait forever."""
    if listing is None or listing.remote_check_status != "pending":
        return
    version = listing.remote_check_version_id
    if version is None:
        return
    if await uow.job_runs.live_for_listing(listing.id, JobKind.LISTING_STATUS_SYNC) is not None:
        return  # a real wait: that job's terminal asks again
    settled = await uow.listings.set_remote_check(
        listing.id, version, "failed", only_version=version, only_status="pending"
    )
    if settled:
        log.info("preview.pending_settled", listing_id=str(listing.id), version_id=str(version))
