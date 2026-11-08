"""`POST /products/{product_id}/size-chart-read` (SI12 B3): read the size-chart photo into a
suggestion, only when the seller asks.

Order (CODE-PLAN T18):
1. A completed replay of the Idempotency-Key answers its stored status and body (the first 202
   stays 202 after the job ends, the photo changes or the product goes); another body under the
   key is 409 `IDEMPOTENCY_KEY_REUSED`.
2. The product (404, tenant-scoped; a foreign one answers as a missing one), a variant is 422
   `FACT_LEVEL_INVALID` (the chart is style-owned), no live size-chart image is 422 at
   `size_chart_image`, no clean derivative yet is 409 `INVALID_STATUS_TRANSITION`.
3. Without a lock: a `read` or `unreadable` result for the current clean hash answers 200 with no
   job; a live job answers 202 with it. A stored `unavailable` or `failed` is not cached.
4. The SI10 admission, outside any unit of work: AI paused is 503 with the pause's code, a daily
   cap 429 `BUDGET_EXCEEDED`. Nothing is written on a refusal.
5. Under the tenant image-job lock and the product image lock the three checks run again, and only
   when all find nothing is one `image_compliance` job enqueued (entity `size_chart_read`).

The read never writes a fact: the job stores its result on the image, and the seller saves the
edited chart through `PATCH /products/{id}` with `size_chart_image_id`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import JobKind

from app.core.errors import ConflictError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.db.models.catalog_assets import Image
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.budget import admit, refuse
from app.services.idempotency import idempotent, key_reused, request_hash
from app.workers.budget import SIZE_CHART_READ_RESERVATION_USD
from app.workers.policy import SIZE_CHART_READ_ENTITY, lock_for

__all__ = ["CACHED", "read_status", "request_read"]

log = get_logger(__name__)
API_V1: Final = "/api/v1"
METHOD: Final = "POST"
# a stored result that answers again without a new job (an `unavailable` or `failed` does not)
CACHED: Final = frozenset({"read", "unreadable"})
LIVE: Final = frozenset({"queued", "running"})


@dataclass(frozen=True, slots=True)
class _Target:
    image_id: uuid.UUID
    source_hash: str
    stored: Mapping[str, Any] | None


def stored_read(image: Image) -> Mapping[str, Any] | None:
    metadata = image.metadata_ if isinstance(image.metadata_, Mapping) else {}
    found = metadata.get("size_chart_read")
    return found if isinstance(found, Mapping) else None


def read_status(stored: Mapping[str, Any] | None, source_hash: str | None) -> str | None:
    """The stored result's status when it was read from this clean copy, else None."""
    if stored is None or source_hash is None or stored.get("source_hash") != source_hash:
        return None
    status = stored.get("status")
    return status if isinstance(status, str) else None


def _body(image_id: uuid.UUID, status: str, job_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "image_id": str(image_id),
        "status": status,
        "job_id": None if job_id is None else str(job_id),
        "poll_url": None if job_id is None else f"{API_V1}/jobs/{job_id}",
    }


async def _target(uow: TenantUnitOfWork, product_id: uuid.UUID) -> _Target:
    product = await uow.products.get_by_id(product_id)
    if product is None:
        raise NotFoundError("product_id")
    if product.parent_product_id is not None:
        raise InputError(ErrorCode.FACT_LEVEL_INVALID, "product_id")
    image = await uow.images.live_size_chart(product_id)
    if image is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "size_chart_image")
    if image.clean_content_hash is None:  # the upload checks have not made its copy yet
        raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "size_chart_image")
    return _Target(image.id, image.clean_content_hash, stored_read(image))


async def _answer(uow: TenantUnitOfWork, target: _Target) -> tuple[int, dict[str, Any]] | None:
    """A cached result (200) or the live job (202), else None (a new job is needed)."""
    status = read_status(target.stored, target.source_hash)
    if status in CACHED:
        return 200, _body(target.image_id, status)
    job = await uow.job_runs.latest_for_entity(SIZE_CHART_READ_ENTITY, target.image_id)
    if job is not None and job.status in LIVE:
        state = "queued" if job.status == "queued" else "reading"
        return 202, _body(target.image_id, state, job.id)
    return None


async def _peek(
    principal: Principal, key: str, path: str, payload: bytes
) -> tuple[int, dict[str, Any]] | None:
    digest = request_hash(METHOD, path, payload)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        seen = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, f"{METHOD} {path}", digest, principal.user_id
        )
    if isinstance(seen, Mismatch):
        raise key_reused()
    if isinstance(seen, Replay):
        return seen.status, seen.body
    return None


async def request_read(
    principal: Principal, product_id: uuid.UUID, idem_key: str, path: str
) -> tuple[int, dict[str, Any]]:
    tenant = principal.tenant_id
    payload = json.dumps({"product_id": str(product_id)}, sort_keys=True).encode()
    # the payload and path name only the product, so the peek needs no `_target`: a replay
    # answers the first response whatever happened to the product or its photos since
    replay = await _peek(principal, idem_key, path, payload)
    if replay is not None:
        return replay
    async with tenant_uow(tenant, user_id=principal.user_id) as uow:
        target = await _target(uow, product_id)
        fast = await _answer(uow, target)
    if fast is not None:
        return fast
    now = datetime.now(UTC)
    refuse(await admit(tenant, now, amount=SIZE_CHART_READ_RESERVATION_USD), now)

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = repositories_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        await uow.images.lock_product_images(product_id)
        current = await _target(uow, product_id)
        again = await _answer(uow, current)
        if again is not None:
            return again
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=SIZE_CHART_READ_ENTITY,
                entity_id=current.image_id,
                created_by=principal.user_id,
                payload={"size_chart_read": True, "image_id": str(current.image_id)},
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, SIZE_CHART_READ_ENTITY, tenant),
            )
        )
        return 202, _body(current.image_id, "queued", enqueued.job.id)

    status, body = await idempotent(principal, idem_key, METHOD, path, payload, handler)
    log.info(
        "size_chart_read.requested",
        tenant_id=str(tenant),
        image_id=body.get("image_id"),
        job_id=body.get("job_id"),
        status=body.get("status"),
    )
    return status, body
