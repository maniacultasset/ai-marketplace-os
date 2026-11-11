"""Image candidates (SI7, ADR-0030 proposed): the pieces the create API and the candidate job
share.

- The cache key of a candidate is (product, kind, `source_hash`, `generator_key`), over every
  row but the retryable failures and the accepted rows (an accepted row answers "already
  applied" only while its image is still the asking colourway's live main, `_still_applied`):
  `source_hash` is the source photo's `clean_content_hash`, or the sha256 of the size chart and
  fabric as the product read shows them; `generator_key` names what makes the image (the fix or
  render version, or the edit model and prompt version), so a new model or version is a new
  candidate and an old one is never reused for it.
- `size_chart_source(facts)` reads the chart through the product read's own parse
  (`size_chart_view`, `fibres_view`), so the API and the job hash the same value.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind

from app.adapters.storage import (
    StorageConfigError,
    StorageNotFound,
    StorageObjectTooLarge,
    get_storage,
    images_bucket,
)
from app.core.config import get_settings
from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.catalog_assets import Image, ImageCandidate
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.image_candidates import NewCandidate
from app.repositories.images import NewImage
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.budget import CreditPausedError, admit
from app.services.idempotency import idempotent, key_reused, request_hash
from app.services.image_keys import CLEAN_NAMES, derived_key, image_object_key
from app.services.images import (
    MAX_LIVE_IMAGES_PER_PRODUCT,
    accepted_body,
    delete_candidate_objects,
    enforce_live_image_cap,
    insert_image_and_job,
    remove_objects_after_commit,
)
from app.services.products import fibres_view, size_chart_view
from app.services.size_chart_image import ChartRefused, layout_size_chart
from app.workers.budget import (
    IMAGE_EDIT_RESERVATION_USD,
    ImageEditLimits,
    image_edit_refusal,
    lock_budget,
    platform_image_spend,
)
from app.workers.policy import CANDIDATE_ENTITY, lock_for

log = get_logger(__name__)

__all__ = [
    "FIX_GENERATOR",
    "IMAGE_EDIT_PROMPT_VERSION",
    "SIZE_CHART_GENERATOR",
    "CandidateRequest",
    "SizeChartSource",
    "candidate_view",
    "create_candidate",
    "edit_generator_key",
    "list_candidates",
    "size_chart_source",
]

FIX_GENERATOR: Final = "fix/v1"
SIZE_CHART_GENERATOR: Final = "size_chart/v1"
# The pipeline owns the prompt; the backend names its version in the cache key only.
IMAGE_EDIT_PROMPT_VERSION: Final = "image_edit/white_bg/v1"


def edit_generator_key(model: str) -> str:
    return f"white_background|{model}|{IMAGE_EDIT_PROMPT_VERSION}"[:200]


@dataclass(frozen=True, slots=True)
class SizeChartSource:
    chart: Mapping[str, Any]
    fibres: Sequence[Mapping[str, Any]] | None
    source_hash: str


def size_chart_source(facts: Mapping[str, Any] | None) -> SizeChartSource | None:
    """The product's size chart and fabric as the product read shows them, and their hash; None
    when the product has no chart."""
    chart = size_chart_view(facts or {})
    if chart is None:
        return None
    fibres = fibres_view(facts or {})
    body = json.dumps(
        {"size_chart": chart, "fibres": fibres}, sort_keys=True, separators=(",", ":")
    )
    return SizeChartSource(
        chart=chart, fibres=fibres, source_hash=hashlib.sha256(body.encode()).hexdigest()
    )


# ---- T13: create and list ----

API_V1: Final = "/api/v1"
PREVIEW_TTL_S: Final = 600
# the failures a create answers as they are (a verdict, cached)
CACHED_FAILURES: Final = frozenset(
    {"fidelity", "not_white", "too_small", "source_unusable", "no_size_chart"}
)


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    kind: str
    source_image_id: uuid.UUID | None


def _accepted(row: ImageCandidate, *, with_job: bool) -> dict[str, Any]:
    job = row.job_id if with_job else None
    applied = row.accepted_image_id if row.status == "accepted" else None
    return {
        "candidate_id": str(row.id),
        "status": row.status,
        "job_id": None if job is None else str(job),
        "poll_url": None if job is None else f"{API_V1}/jobs/{job}",
        "accepted_image_id": None if applied is None else str(applied),
    }


async def _still_applied(
    uow: TenantUnitOfWork, row: ImageCandidate | None, colourway_id: uuid.UUID | None
) -> ImageCandidate | None:
    """ADR-0030 amendment (2026-11-11): an accepted candidate answers a create as "already
    applied" only while its image is live and still holds the place it took: the main of the
    asking product and colourway (the product's size-chart image for a size chart). Otherwise
    it is provenance only and the create makes a new candidate."""
    if row is None or row.status != "accepted" or row.accepted_image_id is None:
        return None
    image = await uow.images.get_live(row.accepted_image_id)
    slot = "size_chart" if row.kind == "size_chart" else "main"
    if (
        image is None
        or image.slot != slot
        or image.product_id != row.product_id
        or image.colourway_id != colourway_id
    ):
        return None
    return row


# a cached row still open for a decision: it answers only for its own live source
_OPEN: Final = frozenset({"queued", "ready", "rejected"})


def _other_source(row: ImageCandidate, body: CandidateRequest) -> bool:
    """The cached row was made from another image with the same pixels (a main replaced by a
    re-upload of the same photo): it can never be accepted for this source (`_still_current`)."""
    return (
        row.source_image_id is not None
        and row.source_image_id != body.source_image_id
        and row.status in _OPEN
    )


async def _cache_answer(uow: TenantUnitOfWork, row: ImageCandidate) -> dict[str, Any] | None:
    """A row the cache index holds answers the create (CP-D17): a `queued` one with its own
    job, a `rejected` one reopened, any other covered row as it is. None: not a cache answer.
    An accepted row is never in the index (0030): `_still_applied` decides for it."""
    if row.status == "queued":
        return _accepted(row, with_job=row.job_id is not None)
    if row.status == "rejected":
        await uow.image_candidates.reopen(row.id)
        fresh = await uow.image_candidates.get(row.id)
        assert fresh is not None  # noqa: S101 - locked under the product lock
        return _accepted(fresh, with_job=False)
    if row.status == "ready" or row.failure_reason in CACHED_FAILURES:
        return _accepted(row, with_job=False)
    return None


async def _audit_candidate(
    uow: TenantUnitOfWork, principal: Principal, action: str, row_id: uuid.UUID, **after: Any
) -> None:
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action=action,
        entity_type="image_candidate",
        entity_id=row_id,
        before=None,
        after=json.dumps(after, sort_keys=True),
    )


@dataclass(frozen=True, slots=True)
class _Key:
    colourway_id: uuid.UUID | None
    source_hash: str
    generator_key: str


async def _resolve_key(
    uow: TenantUnitOfWork, product_id: uuid.UUID, body: CandidateRequest
) -> _Key | ImageCandidate:
    """The product (404), the source or the chart (404/422), and the cache key. A source that
    is no longer the main because a candidate of this kind made from it took its place (a
    click on a page read before the accept) answers that accepted candidate instead of 404."""
    settings = get_settings()
    product = await uow.products.get_by_id(product_id)
    if product is None:
        raise NotFoundError("product_id")
    if body.kind == "size_chart":
        if product.parent_product_id is not None:
            raise InputError(ErrorCode.VALIDATION_FAILED, "product_id")
        source = size_chart_source(product.attributes)
        if source is None:
            raise InputError(ErrorCode.VALIDATION_FAILED, "kind")
        try:
            layout_size_chart(source.chart, source.fibres)
        except ChartRefused:
            raise InputError(ErrorCode.VALIDATION_FAILED, "kind") from None
        return _Key(None, source.source_hash, SIZE_CHART_GENERATOR)
    assert body.source_image_id is not None  # noqa: S101 - ImageCandidateIn
    image = await uow.images.get_live(body.source_image_id)
    if image is None or image.product_id != product_id:
        raise NotFoundError("source_image_id")
    if image.slot != "main":
        made = await uow.image_candidates.latest_accepted_from(image.id, body.kind)
        applied = await _still_applied(uow, made, image.colourway_id)
        if applied is None:
            raise NotFoundError("source_image_id")
        return applied
    if image.clean_content_hash is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "source_image_id")
    generator = (
        edit_generator_key(settings.llm_model_image_edit)
        if body.kind == "white_background"
        else FIX_GENERATOR
    )
    return _Key(image.colourway_id, image.clean_content_hash, generator)


async def create_candidate(
    principal: Principal,
    product_id: uuid.UUID,
    body: CandidateRequest,
    idem_key: str,
    path: str,
) -> dict[str, Any]:
    """`POST /products/{product_id}/image-candidates` under the Idempotency-Key: one candidate
    per cache key, a job only for this call's own new row (CP-D17), the D21 limits before an AI
    job (a refusal is a `failed/budget` row and no job).

    An AI edit also passes the one admission rule (SI10 D4), read before the transaction so it
    holds no pooled connection: with AI paused (or a fresh key check below the floor) a new AI
    job is 503 with the pause's code and nothing is written; a daily cap refusal is a
    `failed/budget` row like D21's. A cached candidate still answers while AI is paused."""
    settings = get_settings()
    if body.kind == "white_background" and not settings.image_edit_enabled:
        raise InputError(ErrorCode.VALIDATION_FAILED, "kind")
    tenant = principal.tenant_id
    now = datetime.now(UTC)
    # an AI edit's two platform reads, before the transaction (no connection held)
    admission, image_spent = None, None
    if body.kind == "white_background":
        admission = await admit(tenant, now, amount=IMAGE_EDIT_RESERVATION_USD)
        image_spent = await platform_image_spend(now.date())
    payload = json.dumps(
        {"kind": body.kind, "source_image_id": str(body.source_image_id or "")}, sort_keys=True
    ).encode()

    retired: list[str] = []

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        await uow.images.lock_product_images(product_id)
        key = await _resolve_key(uow, product_id, body)
        if isinstance(key, ImageCandidate):  # already applied: no new job, no cost
            return 202, _accepted(key, with_job=False)
        cached = await uow.image_candidates.find_cached(
            product_id, body.kind, key.source_hash, key.generator_key
        )
        if cached is not None and _other_source(cached, body):
            # retired out of the cache index, so this source gets its own candidate
            retired.extend(await uow.image_candidates.retire(cached.id))
            cached = None
        if cached is not None:
            answer = await _cache_answer(uow, cached)
            if answer is not None:
                return 202, answer
        if body.kind == "size_chart":  # the same chart, still the product's chart image
            applied = await _still_applied(
                uow,
                await uow.image_candidates.latest_accepted(
                    product_id, body.kind, key.source_hash, key.generator_key
                ),
                None,
            )
            if applied is not None:
                return 202, _accepted(applied, with_job=False)
        new = NewCandidate(
            id=uuid.uuid4(),
            product_id=product_id,
            colourway_id=key.colourway_id,
            source_image_id=body.source_image_id,
            kind=body.kind,
            source_hash=key.source_hash,
            generator_key=key.generator_key,
            created_by=principal.user_id,
        )
        if body.kind == "white_background":
            if admission == "credit_exhausted" or admission == "key_invalid":
                raise CreditPausedError(admission)
            await lock_budget(session, tenant)
            refused = admission or await image_edit_refusal(
                session,
                tenant,
                now.date(),
                ImageEditLimits.from_settings(settings),
                IMAGE_EDIT_RESERVATION_USD,
                platform_spent=image_spent,
            )
            if refused is not None:
                again = await uow.image_candidates.find_refused_today(
                    product_id,
                    body.kind,
                    key.source_hash,
                    key.generator_key,
                    datetime.now(UTC).date(),
                )
                if again is not None:  # a refusal asked again: no new row, no new audit
                    return 202, _accepted(again, with_job=False)
                failed = dataclasses.replace(new, status="failed", failure_reason="budget")
                row, _ = await uow.image_candidates.create_or_get(failed)
                await _audit_candidate(
                    uow,
                    principal,
                    "image_candidate.created",
                    row.id,
                    kind=row.kind,
                    status=row.status,
                )
                return 202, _accepted(row, with_job=False)
        await enforce_live_image_cap(uow.images)
        row, created = await uow.image_candidates.create_or_get(new)
        if not created:  # a concurrent create won: its row answers, with no enqueue
            answer = await _cache_answer(uow, row)
            return 202, answer if answer is not None else _accepted(row, with_job=False)
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=CANDIDATE_ENTITY,
                entity_id=row.id,
                created_by=principal.user_id,
                payload={"candidate_id": str(row.id)},
                max_attempts=1 if body.kind == "white_background" else None,
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, CANDIDATE_ENTITY, tenant),
            )
        )
        if not await uow.image_candidates.set_job(row.id, enqueued.job.id):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "image_candidates")
        await _audit_candidate(
            uow, principal, "image_candidate.created", row.id, kind=row.kind, status="queued"
        )
        return 202, {
            "candidate_id": str(row.id),
            "status": "queued",
            "job_id": str(enqueued.job.id),
            "poll_url": f"{API_V1}/jobs/{enqueued.job.id}",
            "accepted_image_id": None,
        }

    _status, answer = await idempotent(principal, idem_key, "POST", path, payload, handler)
    await delete_candidate_objects(retired)
    log.info(
        "image_candidate.create",
        tenant_id=str(tenant),
        candidate_id=answer.get("candidate_id"),
        job_id=answer.get("job_id"),
        status=answer.get("status"),
    )
    return {**answer, "applied_preview_url": await _applied_preview(principal, answer)}


async def _applied_preview(principal: Principal, answer: Mapping[str, Any]) -> str | None:
    """An "already applied" answer's 10-minute signed GET of the candidate's stored image, so a
    page read before the accept can show the processed photo at once. Signed after the
    idempotent call and never stored with the answer (CP-D18), so a replay signs afresh, and
    only while the image still holds its place (`_still_applied`); None otherwise, or when the
    store is not configured."""
    if answer.get("status") != "accepted" or not answer.get("accepted_image_id"):
        return None
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.image_candidates.get(uuid.UUID(str(answer["candidate_id"])))
        applied = None if row is None else await _still_applied(uow, row, row.colourway_id)
    if applied is None or not applied.storage_key:
        return None
    try:
        storage = get_storage()
    except StorageConfigError:
        log.warning("image_candidate.preview_unsigned", reason="storage_config")
        return None
    return await storage.presign_get(images_bucket(), applied.storage_key, expires_s=PREVIEW_TTL_S)


def _fidelity(row: ImageCandidate) -> dict[str, Any] | None:
    value = row.fidelity_json
    if not isinstance(value, Mapping) or "agreement" not in value:
        return None
    return dict(value)


def _model_and_prompt(row: ImageCandidate) -> tuple[str | None, str | None]:
    parts = row.generator_key.split("|")
    if row.kind == "white_background" and len(parts) == 3:
        return parts[1], parts[2]
    return None, row.generator_key


def candidate_view(
    row: ImageCandidate, *, preview_url: str | None, source_preview_url: str | None
) -> dict[str, Any]:
    model, prompt = _model_and_prompt(row)
    return {
        "id": row.id,
        "kind": row.kind,
        "source_image_id": row.source_image_id,
        "colourway_id": row.colourway_id,
        "status": row.status,
        "failure_reason": row.failure_reason,
        "width": row.width_px,
        "height": row.height_px,
        "fidelity": _fidelity(row),
        "model": model,
        "prompt_version": prompt,
        "cost_usd": row.cost_usd,
        "preview_url": preview_url,
        "source_preview_url": source_preview_url,
        "accepted_image_id": row.accepted_image_id,
        "created_at": row.created_at,
    }


SIGNED_STATUSES: Final = frozenset({"ready", "accepted", "rejected"})


async def list_candidates(principal: Principal, product_id: uuid.UUID) -> list[dict[str, Any]]:
    """The product's candidates, newest first (at most 50), with 10-minute signed previews of
    the stored candidate and of its source's clean copy. A signed URL is never logged or
    stored (`adapters/storage.py`)."""
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        if await uow.products.get_by_id(product_id) is None:
            raise NotFoundError("product_id")
        rows = await uow.image_candidates.list_for_product(product_id)
        sources: dict[uuid.UUID, Image | None] = {}
        for row in rows:
            if row.source_image_id is not None and row.source_image_id not in sources:
                sources[row.source_image_id] = await uow.images.get_any(row.source_image_id)
    storage = get_storage()
    bucket = images_bucket()
    out: list[dict[str, Any]] = []
    for row in rows:
        preview = None
        if row.status in SIGNED_STATUSES and row.storage_key:
            preview = await storage.presign_get(bucket, row.storage_key, expires_s=PREVIEW_TTL_S)
        source_preview = None
        source = None if row.source_image_id is None else sources.get(row.source_image_id)
        if source is not None and source.clean_mime_type in CLEAN_NAMES:
            clean_key = derived_key(
                principal.tenant_id,
                source.product_id,
                source.id,
                CLEAN_NAMES[source.clean_mime_type],
            )
            source_preview = await storage.presign_get(bucket, clean_key, expires_s=PREVIEW_TTL_S)
        out.append(candidate_view(row, preview_url=preview, source_preview_url=source_preview))
    return out


# ---- T14: accept and reject ----

MAX_CANDIDATE_BYTES: Final = 10_485_760


def _not_ready() -> ConflictError:
    return ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "image_candidates")


async def _peek(principal: Principal, key: str, path: str, payload: bytes) -> dict[str, Any] | None:
    """A completed replay's stored body (before any object copy, CODE-PLAN §12 LOW 7); another
    body or member under the key is 409."""
    digest = request_hash("POST", path, payload)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        seen = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, f"POST {path}", digest, principal.user_id
        )
    if isinstance(seen, Mismatch):
        raise key_reused()
    if isinstance(seen, Replay):
        return seen.body
    return None


async def _still_current(uow: TenantUnitOfWork, row: ImageCandidate) -> Image | None:
    """The candidate's source is unchanged since it was made: the same live main (same clean
    hash), or the same size chart. Returns the source image (None for a size chart); 409
    otherwise."""
    if row.kind == "size_chart":
        product = await uow.products.get_by_id(row.product_id)
        source = None if product is None else size_chart_source(product.attributes)
        if source is None or source.source_hash != row.source_hash:
            raise _not_ready()
        return None
    image = None if row.source_image_id is None else await uow.images.get_live(row.source_image_id)
    if (
        image is None
        or image.slot != "main"
        or image.product_id != row.product_id
        or image.clean_content_hash != row.source_hash
    ):
        raise _not_ready()
    return image


async def _audit_image(
    uow: TenantUnitOfWork,
    principal: Principal,
    action: str,
    image_id: uuid.UUID,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action=action,
        entity_type="image",
        entity_id=image_id,
        before=None if before is None else json.dumps(before),
        after=json.dumps(after),
    )


async def accept_candidate(
    principal: Principal, candidate_id: uuid.UUID, idem_key: str, path: str
) -> dict[str, Any]:
    """`POST /image-candidates/{candidate_id}/accept`: the candidate becomes a product image.

    The object is copied to a fresh image key before the transaction (an orphan after a crash
    is swept by the image orphan sweep). Then, under the tenant image-job lock and the product
    lock: the row must be `ready` and its source unchanged (409 otherwise); a main candidate
    demotes the live main to `secondary` (kept, with a re-check job: its report was for main
    rules); a size chart replaces the live size-chart image. The new row carries its provenance
    and gets its own `image_compliance` job. A marketplace sees it only in a new listing version
    another user approves (design §2.8)."""
    tenant = principal.tenant_id
    payload = b""
    replay = await _peek(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:
        pre = await uow.image_candidates.get(candidate_id)
    if pre is None:
        raise NotFoundError("candidate_id")
    if pre.status != "ready" or not pre.storage_key:
        raise _not_ready()
    new_id = uuid.uuid4()
    new_key = image_object_key(tenant, pre.product_id, new_id)
    storage = get_storage()
    try:
        data = await storage.get(images_bucket(), pre.storage_key, max_bytes=MAX_CANDIDATE_BYTES)
    except (StorageNotFound, StorageObjectTooLarge):
        raise _not_ready() from None
    await storage.put(images_bucket(), new_key, data, "image/jpeg")
    replaced: list[uuid.UUID] = []

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        await uow.images.lock_product_images(pre.product_id)
        row = await uow.image_candidates.get_for_update(candidate_id)
        if row is None:
            raise NotFoundError("candidate_id")
        if row.status != "ready":
            raise _not_ready()
        source = await _still_current(uow, row)
        main = row.kind != "size_chart"
        await enforce_live_image_cap(uow.images, adding=2 if main else 1)
        charts = (
            []
            if main
            else [
                d.image_id
                for d in await uow.images.details_for_product(row.product_id)
                if d.slot == "size_chart" and d.colourway_id is None
            ]
        )
        # a new size chart replaces the live one, so the old one leaves the per-product count
        live = await uow.images.count_live(row.product_id) - len(charts)
        if live >= MAX_LIVE_IMAGES_PER_PRODUCT:
            raise InputError(ErrorCode.VALIDATION_FAILED, "images")
        if main:
            assert source is not None  # noqa: S101 - _still_current
            position = await uow.images.next_position(
                row.product_id, source.colourway_id, ImageSlot.SECONDARY
            )
            if not await uow.images.demote_main(source.id, position):
                raise _not_ready()
            await _audit_image(
                uow,
                principal,
                "image.demoted",
                source.id,
                before={"slot": "main"},
                after={"slot": "secondary"},
            )
            await uow.job_runs.enqueue(
                EnqueueSpec(
                    kind=JobKind.IMAGE_COMPLIANCE,
                    entity_type="image",
                    entity_id=source.id,
                    created_by=principal.user_id,
                    payload={"image_id": str(source.id)},
                )
            )
            slot, colourway = ImageSlot.MAIN, source.colourway_id
        else:
            for chart_id in charts:
                await uow.images.soft_delete(chart_id)
                replaced.append(chart_id)
                await _audit_image(
                    uow,
                    principal,
                    "image.replaced",
                    chart_id,
                    before={"deleted_at": None},
                    after={"deleted_at": datetime.now(UTC).isoformat()},
                )
            slot, colourway = ImageSlot.SIZE_CHART, None
        job_id = await insert_image_and_job(
            uow,
            principal,
            NewImage(
                id=new_id,
                product_id=row.product_id,
                colourway_id=colourway,
                storage_key=new_key,
                slot=slot,
                bytes=len(data),
                position=0,
                origin="generated" if row.kind == "white_background" else "rendered",
                parent_image_id=None if source is None else source.id,
                candidate_id=row.id,
            ),
            field="candidate_id",
        )
        if not await uow.image_candidates.mark_accepted(row.id, new_id, principal.user_id):
            raise _not_ready()
        await _audit_candidate(
            uow, principal, "image_candidate.accepted", row.id, kind=row.kind, status="accepted"
        )
        return 202, accepted_body(new_id, job_id)

    _status, body = await idempotent(principal, idem_key, "POST", path, payload, handler)
    for old_id in replaced:
        await remove_objects_after_commit(principal, old_id)
    log.info(
        "image_candidate.accepted",
        tenant_id=str(tenant),
        candidate_id=str(candidate_id),
        image_id=body.get("image_id"),
    )
    return body


def _public_view(row: ImageCandidate) -> dict[str, Any]:
    """The candidate as JSON with no signed URL (CP-D18: an idempotent answer is stored)."""
    view = candidate_view(row, preview_url=None, source_preview_url=None)
    for key in ("id", "source_image_id", "colourway_id", "accepted_image_id"):
        view[key] = None if view[key] is None else str(view[key])
    view["cost_usd"] = format(Decimal(view["cost_usd"]).quantize(Decimal("0.000001")), "f")
    view["created_at"] = view["created_at"].isoformat()
    return view


async def reject_candidate(
    principal: Principal, candidate_id: uuid.UUID, idem_key: str, path: str
) -> dict[str, Any]:
    """`POST /image-candidates/{candidate_id}/reject`: a `ready` candidate becomes `rejected`
    (its object is kept: asking again reopens it, PLAN D10); `rejected` again is the same 200;
    `queued`, `failed` or `accepted` is 409. The answer has no preview URLs (CP-D18)."""
    tenant = principal.tenant_id

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        row = await uow.image_candidates.get_for_update(candidate_id)
        if row is None:
            raise NotFoundError("candidate_id")
        if row.status == "ready":
            await uow.image_candidates.reject(row.id, principal.user_id)
            await _audit_candidate(
                uow, principal, "image_candidate.rejected", row.id, kind=row.kind, status="rejected"
            )
            fresh = await uow.image_candidates.get(row.id)
            assert fresh is not None  # noqa: S101 - locked above
            row = fresh
        elif row.status != "rejected":
            raise _not_ready()
        return 200, _public_view(row)

    _status, body = await idempotent(principal, idem_key, "POST", path, b"", handler)
    return body
