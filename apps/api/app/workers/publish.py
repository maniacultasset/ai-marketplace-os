"""The `listing_publish` job body (S21 T12-T14; DATABASE.md :871-887, :947-949; PLAN §2, D5, D8,
D9, D11; CP1, CP2, CP12; invariants 1, 6, 8, 9, 10).

The only caller of a mutating marketplace adapter (invariant 1), after the API pre-check, the
0004 publish gate and `ls_guard`. No transaction is open during storage or marketplace I/O, and the
listing is locked only in the short write-ahead transaction.

1. **Read** (one short transaction, no lock): the listing (must be `publishing`), its approved =
   current version, the approve decision's `facts_digest`, the open submission, the adapter input
   (`preview_source`), the images, the account and, for a field retry, the retry payload.
2. **Resolve an open row first** (CP1): a `pending` row of this job or of an ended one is
   resolved by reading the marketplace before anything is resent (D8): a `create` whose every SKU
   is found is `accepted`; otherwise this job's own row is resent on the same row and key
   (uncounted), and an ended job's row becomes `error SUBMISSION_NOT_APPLIED` and the next attempt
   follows (its 3rd unresolved attempt sends nothing: `blocked/publish_attempts_exhausted`).
   An older version's row is never resent: found → `accepted` (attempt 1 supersedes it), else
   `error SUBMISSION_NOT_APPLIED`; this version's attempt 1 follows. Once any version reached
   the marketplace, the next send is an `update`, never a `create`.
3. **Pins, outside any transaction** (D9, C:48): the product's facts digest must equal the approve
   row's, and each sent image must be `passed`, carry a `content_hash` (NULL = never checked:
   refused) and re-hash to it (read capped at 10 MiB). A mismatch sends nothing, revalidates the
   listing (approval voided, CP10) and fails the job.
4. **Write-ahead** (Tx A, under the listing lock): re-check the status and the pins, supersede an
   older version's open `accepted` row (attempt 1), INSERT the `pending` row (snapshot written
   first under a pre-minted id, deleted again if Tx A refuses), upsert the items, set the account.
   COMMIT before any marketplace call.
5. **Send**: stamp `sent_at`, open the adapter (`open_adapter`, or the test factory), hand it
   24-hour signed image URLs held in memory only, `create`/`update`/`patch` with the submission key
   and attempt, buffer the raw bodies, store them once.
6. **Route** (`publish_ledger`): every ledger write happens in the wrapper's terminal transaction
   (`persist`), fenced on this job's `pending` row; a definitive RATE_LIMITED first writes the
   `error` row in its own transaction and raises `RetryableJobError`; an ambiguous or connect
   failure leaves the row `pending` for the next run.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.errors import ErrorReport
from mkos_schemas.publish import ListingResult

from app.adapters.base import (
    AdapterError,
    AdapterListing,
    AuthExpired,
    ListingNotFound,
    MarketplaceAdapter,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
    PermissionDenied,
    RateLimited,
    RawResponseSink,
    submitted_fields,
)
from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    get_storage,
    images_bucket,
    submissions_bucket,
)
from app.core.errors import ConflictError, DomainError
from app.core.logging import get_logger
from app.core.resolved_facts import facts_digest
from app.repositories.listings import PreviewSource
from app.repositories.submissions import NewItem, NewSubmission
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services.adapter_input import build_adapter_listing, canonical_listing
from app.services.publish_ledger import Decision, ErrorKind, route_error, route_result
from app.services.publish_outcome import AttemptCtx, apply_decision, refusal_report, revalidate
from app.services.publish_rules import (
    PreviousAttempt,
    choose_justification,
    choose_operation,
    submission_key,
)
from app.services.submission_store import (
    BufferedSink,
    snapshot_bytes,
    write_raw,
    write_snapshot,
)
from app.workers.errors import JobFailed, RetryableJobError
from app.workers.payloads import PublishJobPayload
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)

IMAGE_MAX_BYTES: Final = 10 * 1024 * 1024
IMAGE_URL_TTL_S: Final = 24 * 3600
MAX_SUBMISSIONS: Final = 3
# outcomes after which (part of) the listing is on the marketplace
APPLIED_OUTCOMES: Final = ("partially_accepted", "accepted", "live", "superseded")
TERMINAL_JOB: Final = frozenset({"succeeded", "partial", "failed", "cancelled", "dead_letter"})

AdapterFactory = Callable[[uuid.UUID, uuid.UUID, RawResponseSink], Awaitable[MarketplaceAdapter]]


async def _open_adapter(
    tenant_id: uuid.UUID, account_id: uuid.UUID, sink: RawResponseSink
) -> MarketplaceAdapter:
    from app.workers.adapters import open_adapter

    return await open_adapter(tenant_id, account_id, sink=sink)


_factory: AdapterFactory = _open_adapter


def set_adapter_factory(factory: AdapterFactory | None) -> None:
    """Tests install an in-process adapter (the FakeAdapter, or the AmazonAdapter on the in-process
    fake SP-API); None restores `open_adapter`. Never called by app code."""
    global _factory
    _factory = factory or _open_adapter


@dataclass(frozen=True, slots=True)
class ImageCheck:
    id: uuid.UUID
    index: int
    storage_key: str | None
    content_hash: str | None
    compliance_status: str | None


@dataclass
class Prepared:
    """What the read transaction found (step 1)."""

    listing: Any
    version_id: uuid.UUID
    created_by: uuid.UUID
    account_id: uuid.UUID
    source: PreviewSource
    approve_digest: bytes | None
    facts: bytes | None
    images: list[ImageCheck] = field(default_factory=list)
    open_row: Any | None = None
    open_row_job_ended: bool = False
    prev: Any | None = None
    count: int = 0
    retry_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    justification: str | None = None
    applied_before: bool = False
    listing_applied: bool = False


def _fail(
    code: ErrorCode, message: str, *, field_path: str = "listing", persist: Any = None
) -> JobFailed:
    return JobFailed(refusal_report(code, field_path, message), persist=persist)


# ---- step 1: read ----


async def _read(ctx: JobContext, payload: PublishJobPayload) -> Prepared:
    async with tenant_uow(ctx.tenant_id) as uow:
        job = await uow.job_runs.get_by_id(ctx.job_run_id)
        if job is None or job.created_by is None:
            raise _fail(ErrorCode.INTERNAL_ERROR, "the publish job has no requesting member")
        listing = await uow.listings.get(payload.listing_id)
        if listing is None or listing.status != "publishing":
            raise _fail(ErrorCode.INVALID_STATUS_TRANSITION, "the listing is not publishing")
        version = listing.approved_version_id
        if version is None or version != listing.current_version_id:
            raise _fail(ErrorCode.STALE_VERSION, "the approved version is not current")
        if version != payload.listing_version_id:
            raise _fail(ErrorCode.STALE_VERSION, "the job names another version")
        account = await uow.marketplace_accounts.active_for_publish(
            listing.marketplace, listing.marketplace_account_id
        )
        if account is None:
            raise _fail(ErrorCode.AUTH_EXPIRED, "the marketplace account is not active")
        source = await uow.listings.preview_source(listing.id)
        if source is None or source.version is None:
            raise _fail(ErrorCode.INVALID_STATUS_TRANSITION, "the listing has no version")
        resolved = await uow.product_facts.resolve(listing.product_id)
        prep = Prepared(
            listing=listing,
            version_id=version,
            created_by=job.created_by,
            account_id=account,
            source=source,
            approve_digest=await uow.review_decisions.approve_digest(version),
            facts=None if resolved is None else facts_digest(resolved),
        )
        canonical = canonical_listing(source)
        for index, ref in enumerate(canonical.images):
            row = await uow.images.get_live(ref.image_id)
            prep.images.append(
                ImageCheck(
                    ref.image_id,
                    index,
                    None if row is None else row.storage_key,
                    None if row is None else row.content_hash,
                    None if row is None else row.compliance_status,
                )
            )
        open_row = await uow.submissions.open_for_listing(listing.id)
        if open_row is not None and open_row.outcome == "pending":
            owner = await uow.job_runs.get_by_id(open_row.job_id)
            prep.open_row = open_row
            prep.open_row_job_ended = owner is None or owner.status in TERMINAL_JOB
            if open_row.job_id != ctx.job_run_id and not prep.open_row_job_ended:
                raise _fail(ErrorCode.SUBMISSION_IN_FLIGHT, "another job's submission is open")
        prep.count = await uow.submissions.count_for_version(listing.id, version)
        prep.prev = await uow.submissions.last_for_version(listing.id, version)
        prep.retry_paths = await uow.field_results.retry_payload(listing.id, version)
        prep.applied_before = await uow.submissions.has_outcome(
            listing.id, version, ("partially_accepted",)
        )
        # any version's attempt that reached the marketplace: the next send is never a create
        prep.listing_applied = await uow.submissions.listing_has_outcome(
            listing.id, APPLIED_OUTCOMES
        )
        if prep.prev is not None and prep.prev.outcome != "pending":
            prep.justification = await _justify(uow, listing, prep.prev)
    return prep


async def _justify(uow: TenantUnitOfWork, listing: Any, prev: Any) -> str | None:
    applied = False
    if prev.resolved_at is not None:
        tasks = await uow.listing_issues.task_ids_for_job(prev.job_id)
        applied = await uow.human_tasks.any_applied_after(tasks, prev.resolved_at)
    moved = (
        listing.marketplace_state_changed_at is not None
        and prev.resolved_at is not None
        and listing.marketplace_state_changed_at > prev.resolved_at
    )
    return choose_justification(
        PreviousAttempt(prev.outcome, prev.error_report_json, prev.resolved_at),
        applied_task_after=applied,
        marketplace_changed_after=moved,
    )


# ---- step 3: pins ----


def _revalidate_persist(ctx: JobContext, listing_id: uuid.UUID) -> Any:
    async def persist(uow: TenantUnitOfWork) -> None:
        await revalidate(uow, ctx.job_run_id, listing_id)

    return persist


async def _check_pins(ctx: JobContext, prep: Prepared) -> None:
    persist = _revalidate_persist(ctx, prep.listing.id)
    if prep.facts is None or prep.approve_digest is None or prep.facts != prep.approve_digest:
        raise _fail(
            ErrorCode.FACTS_CHANGED_SINCE_APPROVAL,
            "The product's facts changed after the approval; review the listing again.",
            persist=persist,
        )
    storage = get_storage() if prep.images else None
    for image in prep.images:
        where = f"images[{image.index}]"
        refused = _fail(
            ErrorCode.IMAGE_NON_COMPLIANT,
            "The image is not the checked, compliant image that was approved.",
            field_path=where,
            persist=persist,
        )
        if (
            image.storage_key is None
            or image.content_hash is None
            or image.compliance_status != "passed"
        ):
            raise refused
        if storage is None:  # unreachable: set whenever there are images
            raise refused
        try:
            data = await storage.get(images_bucket(), image.storage_key, max_bytes=IMAGE_MAX_BYTES)
        except (StorageNotFound, StorageObjectTooLarge):
            raise refused from None
        if hashlib.sha256(data).hexdigest() != image.content_hash:
            raise refused


# ---- step 4: write-ahead ----


@dataclass(frozen=True, slots=True)
class Attempt:
    submission_id: uuid.UUID
    attempt: int
    operation: str
    key: str
    per_sku: dict[str, tuple[str, ...]] | None
    justification: str | None
    previous_id: uuid.UUID | None


def _items(source: PreviewSource) -> list[NewItem]:
    product = source.product
    if product.variation_theme is None:
        return [NewItem(product.id, "standalone", product.sku)]
    return [
        NewItem(product.id, "parent", product.sku),
        *(NewItem(c.id, "child", c.sku) for c in source.children),
    ]


def _image_tags(prep: Prepared) -> dict[uuid.UUID, str]:
    return {i.id: f"image:{i.id}@{i.content_hash}" for i in prep.images}


async def _write_ahead(ctx: JobContext, prep: Prepared, plan: Attempt, snapshot: bytes) -> None:
    storage = get_storage()
    key, digest = await write_snapshot(storage, ctx.tenant_id, plan.submission_id, snapshot)
    try:
        async with tenant_uow(ctx.tenant_id, user_id=prep.created_by) as uow:
            listing = await uow.listings.get_for_update(prep.listing.id)
            if listing is None or listing.status != "publishing":
                raise _fail(ErrorCode.INVALID_STATUS_TRANSITION, "the listing is not publishing")
            if (
                listing.approved_version_id != prep.version_id
                or listing.current_version_id != prep.version_id
            ):
                raise _fail(ErrorCode.STALE_VERSION, "the approved version changed")
            if await uow.review_decisions.approve_digest(prep.version_id) != prep.approve_digest:
                raise _fail(ErrorCode.FACTS_CHANGED_SINCE_APPROVAL, "the approval changed")
            for image in prep.images:
                row = await uow.images.get_live(image.id)
                if row is None or row.content_hash != image.content_hash:
                    raise _fail(
                        ErrorCode.IMAGE_NON_COMPLIANT,
                        "The image changed after the check.",
                        field_path=f"images[{image.index}]",
                        persist=_revalidate_persist(ctx, prep.listing.id),
                    )
            if plan.attempt == 1:
                await uow.submissions.supersede_open_accepted(listing.id, prep.version_id)
            await uow.submissions.insert_pending(
                NewSubmission(
                    id=plan.submission_id,
                    listing_id=listing.id,
                    listing_version_id=prep.version_id,
                    marketplace=listing.marketplace,
                    marketplace_account_id=prep.account_id,
                    job_id=ctx.job_run_id,
                    attempt=plan.attempt,
                    operation=plan.operation,
                    payload_sha256=digest,
                    payload_storage_key=key,
                    facts_digest=prep.approve_digest or b"",
                    idempotency_key=plan.key,
                    requested_by_user_id=prep.created_by,
                    field_paths=(
                        None
                        if plan.per_sku is None
                        else sorted({p for paths in plan.per_sku.values() for p in paths})
                    ),
                    retry_justification=plan.justification,
                    previous_submission_id=plan.previous_id,
                )
            )
            await uow.listing_items.upsert_for_submission(
                listing.id, listing.marketplace, _items(prep.source)
            )
            if listing.marketplace_account_id is None:
                await uow.listings.set_columns(listing.id, marketplace_account_id=prep.account_id)
    except (JobFailed, DomainError) as exc:
        try:
            await storage.delete(submissions_bucket(), key)
        except Exception as cleanup:  # an orphan under the tenant prefix is tolerated, logged
            log.warning("publish.snapshot_orphan", error_class=type(cleanup).__name__)
        if isinstance(exc, JobFailed):
            raise
        raise _db_refusal(exc) from None


_GUARD_MESSAGES: Final[Mapping[ErrorCode, str]] = {
    ErrorCode.SUBMISSION_IN_FLIGHT: "Another submission of this listing is open.",
    ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED: "This version was already submitted 3 times.",
    ErrorCode.DUPLICATE_SUBMISSION_BLOCKED: "The retry is not justified.",
    ErrorCode.FACTS_CHANGED_SINCE_APPROVAL: "The facts changed after the approval.",
}


def _db_refusal(exc: DomainError) -> JobFailed:
    code = exc.code if isinstance(exc, ConflictError) else ErrorCode.INTERNAL_ERROR
    if code is ErrorCode.INTERNAL_ERROR:
        code = ErrorCode.INVALID_STATUS_TRANSITION  # a GRD01 refusal of ls_guard or li_guard
    return _fail(code, _GUARD_MESSAGES.get(code, "The submission was refused."))


# ---- step 5: send ----


def _fields_for(item: AdapterListing) -> dict[str, list[str]]:
    paths = submitted_fields(item.listing)
    return {u.seller_sku: list(paths) for u in item.units()}


async def _image_urls(prep: Prepared) -> dict[uuid.UUID, str]:
    if not prep.images:
        return {}
    storage = get_storage()
    return {
        image.id: await storage.presign_get(
            images_bucket(), image.storage_key or "", expires_s=IMAGE_URL_TTL_S
        )
        for image in prep.images
    }


async def _mark_sent(ctx: JobContext, prep: Prepared, submission_id: uuid.UUID) -> None:
    async with tenant_uow(ctx.tenant_id, user_id=prep.created_by) as uow:
        await uow.submissions.mark_sent(submission_id)


async def _send(
    adapter: MarketplaceAdapter, item: AdapterListing, operation: str, per_sku: Any
) -> ListingResult:
    if operation == "create":
        return await adapter.create_listing(item)
    fields: Mapping[str, Sequence[str]] = per_sku if per_sku else _fields_for(item)
    return await adapter.update_listing(item, fields=fields)


async def _read_all_found(adapter: MarketplaceAdapter, item: AdapterListing) -> bool:
    """D8: a `create` is applied when the marketplace knows every SKU it sent."""
    for unit in item.units():
        try:
            await adapter.get_listing(unit.seller_sku, listing_id=item.listing_id)
        except ListingNotFound:
            return False
    return True


async def _aclose(adapter: MarketplaceAdapter) -> None:
    close = getattr(adapter, "aclose", None)
    if close is not None:
        await close()


# ---- the body ----


def _plan_new(ctx: JobContext, prep: Prepared, payload: PublishJobPayload) -> Attempt:
    attempt = prep.count + 1
    if attempt > MAX_SUBMISSIONS:
        raise _fail(ErrorCode.SUBMISSION_ATTEMPTS_EXHAUSTED, "This version was submitted 3 times.")
    justification: str | None = None
    previous: uuid.UUID | None = None
    if attempt > 1:
        if prep.prev is None or prep.justification is None:
            raise _fail(ErrorCode.DUPLICATE_SUBMISSION_BLOCKED, "The retry is not justified.")
        justification, previous = prep.justification, prep.prev.id
    # a field retry, the next attempt after an unapplied patch, and any attempt (`resume`, or
    # `publish` after `publish_failed`) once this version is partly applied resend only the
    # failed fields (invariant 10, L12033); with nothing failed left it is a full update
    keep_patch = prep.prev is not None and prep.prev.operation == "patch"
    partly_applied = prep.applied_before or (
        prep.prev is not None and prep.prev.outcome == "partially_accepted"
    )
    per_sku = (
        dict(prep.retry_paths)
        if (payload.mode == "retry_fields" or keep_patch or partly_applied) and prep.retry_paths
        else None
    )
    previously = (
        prep.listing.published_version_id is not None or prep.applied_before or prep.listing_applied
    )
    operation = choose_operation(
        previously_accepted=previously,
        retry_paths=[p for paths in (per_sku or {}).values() for p in paths],
    )
    return Attempt(
        submission_id=uuid.uuid4(),
        attempt=attempt,
        operation=operation,
        key="",
        per_sku=per_sku,
        justification=justification,
        previous_id=previous,
    )


def _ctx_for(
    ctx: JobContext,
    prep: Prepared,
    submission_id: uuid.UUID,
    item: AdapterListing,
    per_sku: Any,
    *,
    fence_job: bool = True,
) -> AttemptCtx:
    sent = per_sku if per_sku else _fields_for(item)
    return AttemptCtx(
        job_id=ctx.job_run_id,
        tenant_id=ctx.tenant_id,
        listing_id=prep.listing.id,
        version_id=prep.version_id,
        submission_id=submission_id,
        account_id=prep.account_id,
        seller_sku=item.seller_sku,
        sent_paths=tuple(sorted({p for paths in sent.values() for p in paths})),
        fence_job=fence_job,
    )


def _finish(attempt_ctx: AttemptCtx, decision: Decision) -> Outcome:
    async def persist(uow: TenantUnitOfWork) -> None:
        await apply_decision(uow, attempt_ctx, decision)

    result = {
        "submission_id": str(attempt_ctx.submission_id),
        "outcome": decision.outcome,
        "listing_status": decision.listing_to,
    }
    if decision.job_status == "succeeded":
        return Outcome("succeeded", result=result, persist=persist)
    if decision.job_status == "partial":
        return Outcome("partial", result=result, persist=persist, error=decision.report)
    report = decision.report or refusal_report(
        decision.error_code or ErrorCode.PUBLISH_FAILED, "listing", "The publish failed."
    )
    raise JobFailed(report, persist=persist)


async def _resolve_error_now(
    ctx: JobContext, prep: Prepared, attempt_ctx: AttemptCtx, decision: Decision
) -> None:
    """Tx B (CP2): a definitive RATE_LIMITED is written before the job is retried."""
    async with tenant_uow(ctx.tenant_id, user_id=prep.created_by) as uow:
        await apply_decision(uow, attempt_ctx, decision)


_ERROR_KINDS: Final[tuple[tuple[type[AdapterError], ErrorKind], ...]] = (
    (RateLimited, "rate_limited"),
    (AuthExpired, "auth_expired"),
    (PermissionDenied, "permission_denied"),
    (MarketplaceAmbiguous, "ambiguous"),
    (MarketplaceUnavailable, "unavailable"),
)


def _kind_of(exc: AdapterError) -> ErrorKind:
    for cls, kind in _ERROR_KINDS:
        if isinstance(exc, cls):
            return kind
    return "unavailable"


async def run_listing_publish(ctx: JobContext) -> Outcome:
    payload = PublishJobPayload.model_validate(ctx.payload)
    prep = await _read(ctx, payload)
    await _check_pins(ctx, prep)
    sink = BufferedSink()
    adapter = await _factory(ctx.tenant_id, prep.account_id, sink)
    try:
        return await _publish(ctx, prep, payload, adapter, sink)
    finally:
        await _aclose(adapter)


async def _publish(
    ctx: JobContext,
    prep: Prepared,
    payload: PublishJobPayload,
    adapter: MarketplaceAdapter,
    sink: BufferedSink,
) -> Outcome:
    urls = await _image_urls(prep)
    open_row = prep.open_row
    if open_row is not None:
        resent = await _resolve_open(ctx, prep, adapter, sink, urls)
        if resent is not None:
            return resent
        prep = await _read(ctx, payload)  # the row is resolved: plan the next attempt afresh
    plan = _plan_new(ctx, prep, payload)
    snapshot_item = build_adapter_listing(
        prep.source, image_refs={}, idempotency_key="pending", attempt=plan.attempt
    )
    snapshot = snapshot_bytes(
        snapshot_item,
        operation=plan.operation,
        field_paths=plan.per_sku,
        image_tags=_image_tags(prep),
    )
    digest = hashlib.sha256(snapshot).digest()
    key = submission_key(
        ctx.tenant_id,
        prep.listing.id,
        prep.version_id,
        prep.listing.marketplace,
        plan.attempt,
        digest,
    )
    plan = Attempt(
        plan.submission_id,
        plan.attempt,
        plan.operation,
        key,
        plan.per_sku,
        plan.justification,
        plan.previous_id,
    )
    await _write_ahead(ctx, prep, plan, snapshot)
    item = build_adapter_listing(
        prep.source, image_refs=urls, idempotency_key=key, attempt=plan.attempt
    )
    return await _call(
        ctx,
        prep,
        adapter,
        sink,
        item,
        plan.submission_id,
        plan.operation,
        plan.per_sku,
        plan.attempt,
    )


async def _call(
    ctx: JobContext,
    prep: Prepared,
    adapter: MarketplaceAdapter,
    sink: BufferedSink,
    item: AdapterListing,
    submission_id: uuid.UUID,
    operation: str,
    per_sku: dict[str, tuple[str, ...]] | None,
    attempt: int,
    *,
    fence_job: bool = True,
) -> Outcome:
    attempt_ctx = _ctx_for(ctx, prep, submission_id, item, per_sku, fence_job=fence_job)
    await _mark_sent(ctx, prep, submission_id)
    try:
        result = await _send(adapter, item, operation, per_sku)
    except AdapterError as exc:
        return await _on_error(ctx, prep, attempt_ctx, sink, exc, attempt)
    except ListingNotFound:
        report = refusal_report(
            ErrorCode.SUBMISSION_NOT_APPLIED,
            "listing",
            "The marketplace does not know this listing; nothing was applied.",
        )
        decision = Decision(
            outcome="error",
            job_status="failed",
            error_code=ErrorCode.SUBMISSION_NOT_APPLIED,
            report=report.model_copy(update={"retryable": True}),
            listing_event="submission_failed",
            listing_to="failed",
            reason="publish_failed",
        )
        return _finish(await _with_raw(ctx, attempt_ctx, sink), decision)
    attempt_ctx = await _with_raw(ctx, attempt_ctx, sink)
    decision = route_result(result, attempt=attempt)
    log.info(
        "publish.sent",
        job_run_id=str(ctx.job_run_id),
        listing_id=str(prep.listing.id),
        attempt=attempt,
        operation=operation,
        status=result.status,
    )
    return _finish(attempt_ctx, decision)


async def _with_raw(ctx: JobContext, attempt_ctx: AttemptCtx, sink: BufferedSink) -> AttemptCtx:
    stored = await write_raw(get_storage(), ctx.tenant_id, attempt_ctx.submission_id, sink)
    sink.entries.clear()
    if stored is None:
        return attempt_ctx
    key, sha = stored
    return AttemptCtx(
        attempt_ctx.job_id,
        attempt_ctx.tenant_id,
        attempt_ctx.listing_id,
        attempt_ctx.version_id,
        attempt_ctx.submission_id,
        attempt_ctx.account_id,
        attempt_ctx.seller_sku,
        attempt_ctx.sent_paths,
        key,
        sha,
        attempt_ctx.fence_job,
    )


async def _on_error(
    ctx: JobContext,
    prep: Prepared,
    attempt_ctx: AttemptCtx,
    sink: BufferedSink,
    exc: AdapterError,
    attempt: int,
) -> Outcome:
    kind = _kind_of(exc)
    log.info(
        "publish.adapter_error",
        job_run_id=str(ctx.job_run_id),
        listing_id=str(prep.listing.id),
        kind=kind,
        attempt=attempt,
    )
    decision = route_error(kind, exc.report, attempt=attempt)
    if decision.outcome == "pending":
        # ambiguous or nothing sent: the row stays pending, the next run reads first (CP1)
        raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE)
    attempt_ctx = await _with_raw(ctx, attempt_ctx, sink)
    if decision.job_status == "retry":
        await _resolve_error_now(ctx, prep, attempt_ctx, decision)
        raise RetryableJobError(decision.error_code or ErrorCode.RATE_LIMITED)
    return _finish(attempt_ctx, decision)


async def _resolve_open(
    ctx: JobContext,
    prep: Prepared,
    adapter: MarketplaceAdapter,
    sink: BufferedSink,
    urls: Mapping[uuid.UUID, str],
) -> Outcome | None:
    """CP1 for an open `pending` row. Returns the job's outcome when this run resent or
    resolved it as applied; None when the row became `error SUBMISSION_NOT_APPLIED` and the
    next attempt follows."""
    row = prep.open_row
    if row is None:  # the caller checked: nothing to resolve
        return None
    if row.listing_version_id != prep.version_id:
        await _resolve_foreign(ctx, prep, adapter, urls, row)
        return None
    per_sku: dict[str, tuple[str, ...]] | None = None
    if row.operation == "patch":
        async with tenant_uow(ctx.tenant_id) as uow:
            per_sku = await uow.field_results.retry_payload(prep.listing.id, prep.version_id)
    item = build_adapter_listing(
        prep.source, image_refs=urls, idempotency_key=row.idempotency_key, attempt=row.attempt
    )
    own = row.job_id == ctx.job_run_id
    if row.sent_at is not None and row.operation == "create":
        try:
            found = await _read_all_found(adapter, item)
        except AdapterError as exc:
            log.info("publish.read_failed", job_run_id=str(ctx.job_run_id), kind=_kind_of(exc))
            raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
        if found:
            attempt_ctx = _ctx_for(ctx, prep, row.id, item, per_sku, fence_job=own)
            decision = Decision(
                outcome="accepted",
                job_status="succeeded",
                listing_event="submission_accepted",
                listing_to="published",
            )
            log.info("publish.read_found_applied", job_run_id=str(ctx.job_run_id))
            return _finish(attempt_ctx, decision)
    if own or row.sent_at is None:
        # the same row and key, uncounted (T19): PUT/PATCH are idempotent (D8)
        return await _call(
            ctx,
            prep,
            adapter,
            sink,
            item,
            row.id,
            row.operation,
            per_sku,
            row.attempt,
            fence_job=own,
        )
    # an ended job's row that was not applied: SUBMISSION_NOT_APPLIED, then attempt n+1
    report = refusal_report(
        ErrorCode.SUBMISSION_NOT_APPLIED,
        "listing",
        "The earlier attempt was not applied by the marketplace; it is retried.",
        attempt=row.attempt,
    ).model_copy(update={"retryable": True})
    async with tenant_uow(ctx.tenant_id, user_id=prep.created_by) as uow:
        await uow.submissions.resolve(
            row.id,
            outcome="error",
            error_code=ErrorCode.SUBMISSION_NOT_APPLIED.value,
            report=report.model_dump(mode="json"),
        )
    log.info("publish.read_not_applied", job_run_id=str(ctx.job_run_id), attempt=row.attempt)
    if row.attempt >= MAX_SUBMISSIONS:
        attempt_ctx = _ctx_for(ctx, prep, row.id, item, per_sku, fence_job=False)
        decision = Decision(
            outcome="error",
            job_status="failed",
            error_code=ErrorCode.SUBMISSION_NOT_APPLIED,
            report=report,
            listing_event="submission_attempts_exhausted",
            listing_to="blocked",
            reason="publish_attempts_exhausted",
        )
        return _exhausted(attempt_ctx, decision)
    return None


async def _resolve_foreign(
    ctx: JobContext,
    prep: Prepared,
    adapter: MarketplaceAdapter,
    urls: Mapping[uuid.UUID, str],
    row: Any,
) -> None:
    """An older version's `pending` row (its job ended) while this job publishes a newer one:
    never resent with this version's content, and never resolved into this version's ledger.
    A sent `create` whose every SKU is found becomes `accepted` without moving the listing (the
    next attempt 1's write-ahead supersedes it); anything else becomes `error
    SUBMISSION_NOT_APPLIED`. The 3-attempt count is per version, so no exhaustion here."""
    outcome = "error"
    if row.sent_at is not None and row.operation == "create":
        item = build_adapter_listing(
            prep.source, image_refs=urls, idempotency_key=row.idempotency_key, attempt=row.attempt
        )
        try:
            found = await _read_all_found(adapter, item)
        except AdapterError as exc:
            log.info("publish.read_failed", job_run_id=str(ctx.job_run_id), kind=_kind_of(exc))
            raise RetryableJobError(ErrorCode.MARKETPLACE_UNAVAILABLE) from None
        if found:
            outcome = "accepted"
    report = None
    if outcome == "error":
        report = refusal_report(
            ErrorCode.SUBMISSION_NOT_APPLIED,
            "listing",
            "An earlier version's attempt was not applied by the marketplace.",
            attempt=row.attempt,
        ).model_copy(update={"retryable": True})
    async with tenant_uow(ctx.tenant_id, user_id=prep.created_by) as uow:
        await uow.submissions.resolve(
            row.id,
            outcome=outcome,
            error_code=None if report is None else ErrorCode.SUBMISSION_NOT_APPLIED.value,
            report=None if report is None else report.model_dump(mode="json"),
        )
    log.info("publish.foreign_row_resolved", job_run_id=str(ctx.job_run_id), outcome=outcome)


def _exhausted(attempt_ctx: AttemptCtx, decision: Decision) -> Outcome:
    """The row is already resolved: only the listing moves (no resolve, no field rows)."""
    from app.services.publish_outcome import move_only

    async def persist(uow: TenantUnitOfWork) -> None:
        await move_only(uow, attempt_ctx, decision)

    raise JobFailed(decision.report or ErrorReport(status="failed"), persist=persist)
