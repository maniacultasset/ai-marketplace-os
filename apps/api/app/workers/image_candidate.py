"""The image candidate job body (SI7 T11-T12, ADR-0030 proposed): `run_image_candidate(ctx)`.

A candidate job is an `image_compliance` job whose payload names a `candidate_id`
(`app.workers.image_jobs` dispatches). Steps for the zero-token kinds (`fix`, `size_chart`):

1. One short read: the candidate (gone, owned by another job or not `queued` → `succeeded`
   with `skipped`); for `fix` the source row (still the live `main`, same `clean_content_hash`,
   else `failed/source_unusable`); for `size_chart` the product's chart (none → `no_size_chart`;
   changed → `source_unusable`).
2. GET the clean copy (10 MiB cap; a store outage is retried).
3. The child op in the killable child. A verdict is stored (`too_small`, `source_unusable`,
   `not_white`); an infrastructure failure (`CandidateChildFailed`) is retried, and after the
   last attempt the terminal hook ends the row `failed/model_failed` (CP-D19: never cached).
4. PUT the object under `candidate_key` (an outage is retried).
5. `mark_ready` (fenced on this job and `queued`), then the outcome.

No model call and no `agent_runs` row for these kinds. Log lines carry ids, the op and the
reason only, never pixels, keys or signed URLs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind, Marketplace

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
)
from app.core.logging import get_logger
from app.db.models.catalog_assets import Image, ImageCandidate
from app.repositories.uow import TenantUnitOfWork, tenant_uow
from app.services import image_runner
from app.services.image_candidates import size_chart_source
from app.services.image_keys import CLEAN_NAMES, candidate_key, derived_key
from app.services.image_runner import CandidateChildFailed, CandidateReply
from app.services.rules_loader import RuleLoadError, load_rules
from app.workers.errors import JobFailed, RetryableJobError, system_report
from app.workers.policy import POLICIES
from app.workers.registry import JobContext, Outcome

log = get_logger(__name__)
MAX_IMAGE_BYTES: Final = 10_485_760


def candidate_id_of(ctx: JobContext) -> UUID:
    try:
        return UUID(str(ctx.payload["candidate_id"]))
    except (KeyError, ValueError):
        raise _failed("the candidate job has no candidate id", ctx) from None


def _failed(message: str, ctx: JobContext) -> JobFailed:
    return JobFailed(
        system_report(
            ErrorCode.INTERNAL_ERROR,
            message,
            attempt=ctx.attempt,
            max_attempts=POLICIES[JobKind.IMAGE_COMPLIANCE].max_attempts,
            request_id=ctx.request_id,
        )
    )


def _done(candidate_id: UUID, status: str, **extra: Any) -> Outcome:
    return Outcome("succeeded", {"candidate_id": str(candidate_id), "status": status, **extra})


async def main_rules(uow: TenantUnitOfWork, ctx: JobContext, category: str) -> dict[str, Any]:
    """The resolved `images` rules per marketplace, as JSON for the child (as the compliance job
    loads them)."""
    out: dict[str, Any] = {}
    for market in Marketplace:
        try:
            loaded = await load_rules(
                uow, tenant_id=ctx.tenant_id, marketplace=market, category=category
            )
        except RuleLoadError:
            raise _failed("the image rules cannot be loaded", ctx) from None
        out[market.value] = [
            r.model_dump(mode="json", by_alias=True)
            for r in loaded.ruleset.rules
            if r.field == "images"
        ]
    return out


async def fail(ctx: JobContext, candidate_id: UUID, reason: str, **extra: Any) -> Outcome:
    async with tenant_uow(ctx.tenant_id) as uow:
        await uow.image_candidates.mark_failed(
            candidate_id, ctx.job_run_id, reason, fidelity=extra.get("fidelity")
        )
    log.info(
        "image_candidate.failed",
        candidate_id=str(candidate_id),
        job_run_id=str(ctx.job_run_id),
        reason=reason,
    )
    return _done(candidate_id, "failed", reason=reason)


async def read_object(key: str) -> bytes:
    try:
        return await get_storage().get(images_bucket(), key, max_bytes=MAX_IMAGE_BYTES)
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None


def clean_key_of(tenant_id: UUID, source: Image) -> str | None:
    if source.clean_mime_type not in CLEAN_NAMES:
        return None
    return derived_key(tenant_id, source.product_id, source.id, CLEAN_NAMES[source.clean_mime_type])


def source_is_current(candidate: ImageCandidate, source: Image | None) -> bool:
    """The source is still the product's live main with the hash the candidate was made for."""
    return (
        source is not None
        and source.deleted_at is None
        and source.slot == "main"
        and source.product_id == candidate.product_id
        and source.clean_content_hash == candidate.source_hash
    )


async def store_ready(
    ctx: JobContext,
    candidate: ImageCandidate,
    reply: CandidateReply,
    *,
    fidelity: dict[str, Any] | None = None,
) -> Outcome:
    assert reply.image is not None and reply.width and reply.height  # noqa: S101 - an ok reply
    key = candidate_key(ctx.tenant_id, candidate.product_id, candidate.id)
    try:
        await get_storage().put(images_bucket(), key, reply.image, "image/jpeg")
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    async with tenant_uow(ctx.tenant_id) as uow:
        written = await uow.image_candidates.mark_ready(
            candidate.id,
            ctx.job_run_id,
            storage_key=key,
            width=reply.width,
            height=reply.height,
            fidelity=fidelity,
        )
    log.info(
        "image_candidate.ready",
        candidate_id=str(candidate.id),
        job_run_id=str(ctx.job_run_id),
        kind=candidate.kind,
        written=written,
    )
    return _done(candidate.id, "ready" if written else "skipped")


async def _deterministic(ctx: JobContext, candidate: ImageCandidate) -> Outcome:
    """`fix` and `size_chart`: zero tokens, retried on infrastructure failures."""
    inputs: dict[str, bytes] = {}
    params: dict[str, Any]
    async with tenant_uow(ctx.tenant_id) as uow:
        product = await uow.products.get_by_id(candidate.product_id)
        if product is None:
            return await fail(ctx, candidate.id, "source_unusable")
        if candidate.kind == "size_chart":
            source = size_chart_source(product.attributes)
            if source is None:
                return await fail(ctx, candidate.id, "no_size_chart")
            if source.source_hash != candidate.source_hash:
                return await fail(ctx, candidate.id, "source_unusable")
            params = {"chart": dict(source.chart), "fibres": source.fibres}
            op = "size_chart"
            clean_key = None
        else:
            row = (
                None
                if candidate.source_image_id is None
                else await uow.images.get_live(candidate.source_image_id)
            )
            if not source_is_current(candidate, row):
                return await fail(ctx, candidate.id, "source_unusable")
            assert row is not None  # noqa: S101 - source_is_current
            clean_key = clean_key_of(ctx.tenant_id, row)
            if clean_key is None:
                return await fail(ctx, candidate.id, "source_unusable")
            params = {
                "rules": await main_rules(uow, ctx, product.category),
                "image_id": str(candidate.id),
                "now": datetime.now(UTC).isoformat(),
            }
            op = "fix"
    if clean_key is not None:
        try:
            inputs["clean"] = await read_object(clean_key)
        except (StorageNotFound, StorageObjectTooLarge):
            return await fail(ctx, candidate.id, "source_unusable")
    try:
        reply = await image_runner.run_candidate_isolated(op, inputs, params)
    except CandidateChildFailed as error:
        log.info(
            "image_candidate.child_failed",
            candidate_id=str(candidate.id),
            job_run_id=str(ctx.job_run_id),
            op=op,
            reason=error.reason,
        )
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    if not reply.ok:
        return await fail(ctx, candidate.id, reply.reason or "source_unusable")
    return await store_ready(ctx, candidate, reply)


async def run_image_candidate(ctx: JobContext) -> Outcome:
    candidate_id = candidate_id_of(ctx)
    async with tenant_uow(ctx.tenant_id) as uow:
        candidate = await uow.image_candidates.get(candidate_id)
    if candidate is None:
        return _done(candidate_id, "skipped", skipped="candidate_gone")
    if candidate.job_id != ctx.job_run_id or candidate.status != "queued":
        return _done(candidate_id, "skipped", skipped=candidate.status)
    if candidate.kind == "white_background":
        return await _white_background(ctx, candidate)
    return await _deterministic(ctx, candidate)


async def _white_background(ctx: JobContext, candidate: ImageCandidate) -> Outcome:
    """The AI path (T12)."""
    return await fail(ctx, candidate.id, "model_failed")
