"""The image upload and report services (S17b T8-T12; PLAN D1, D2, D7-D9, D13-D15).

- `mint_upload_url`: a create-only signed PUT for a fresh image key under the caller's tenant
  and the given product (no DB write, no audit). The client must send every returned header.
- `accept_image_upload`: register an object (a `storage_key` from the signed step, or a
  multipart `file` stored here) as an `images` row and start its `image_compliance` job, under
  the Idempotency-Key. No transaction is open while the object store is called.
- `get_report`: the stored per-marketplace reports of a live image.

A key is read only through `parse_image_key`: another tenant's prefix is 404 (the same bytes as
a missing key), never a string-prefix test. Logs carry ids and statuses only, never a signed URL.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, JobKind, Marketplace
from mkos_schemas.images import ImageReport

from app.adapters.storage import get_storage, images_bucket
from app.core.errors import DomainError, InputError, LimitError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.idempotency import IdempotencyKeyRepository, Mismatch, Replay
from app.repositories.images import ImageRepository, NewImage
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork, repositories_on, tenant_uow
from app.services.idempotency import idempotent, key_reused, request_hash
from app.services.image_keys import image_object_key, parse_image_key

log = get_logger(__name__)
API_V1: Final = "/api/v1"
UPLOAD_URL_TTL_S: Final = 900
MAX_IMAGE_BYTES: Final = 10_485_760
REGISTER_WINDOW: Final = timedelta(hours=23)
MAX_LIVE_IMAGES_PER_PRODUCT: Final = 50
LIVE_IMAGE_JOB_CAP: Final = 20
IMAGE_JOB_LOCK: Final = "image_compliance"
MULTIPART_CONTENT_TYPE: Final = "application/octet-stream"


class ImageTooLargeError(DomainError):
    """The image is over `MAX_IMAGE_BYTES` (413 `PAYLOAD_TOO_LARGE`)."""


@dataclass(frozen=True, slots=True)
class UploadUrl:
    upload_url: str
    storage_key: str
    expires_at: datetime
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ImageUploadForm:
    product_id: uuid.UUID
    slot: ImageSlot
    colourway_id: uuid.UUID | None
    storage_key: str | None


@dataclass(frozen=True, slots=True)
class ReportView:
    image_id: uuid.UUID
    compliance_status: str
    reports: dict[Marketplace, ImageReport]


async def _check_product(
    images: ImageRepository, products: Any, product_id: uuid.UUID, slot: ImageSlot
) -> Any:
    """The live product (else 404 `product_id`); a size chart only on a style or standalone."""
    product = await products.get_by_id(product_id)
    if product is None:
        raise NotFoundError("product_id")
    if slot is ImageSlot.SIZE_CHART and product.parent_product_id is not None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "slot")
    return product


# ---- T8: the signed upload URL ----


async def mint_upload_url(
    principal: Principal, *, product_id: uuid.UUID, slot: ImageSlot, content_type: str, size: int
) -> UploadUrl:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await _check_product(uow.images, uow.products, product_id, slot)
    image_id = uuid.uuid4()
    key = image_object_key(principal.tenant_id, product_id, image_id)
    url = await get_storage().presign_put(
        images_bucket(),
        key,
        content_type=content_type,
        content_length=size,
        expires_s=UPLOAD_URL_TTL_S,
    )
    log.info("image.upload_url", tenant_id=str(principal.tenant_id), image_id=str(image_id))
    return UploadUrl(
        upload_url=url,
        storage_key=key,
        expires_at=datetime.now(UTC) + timedelta(seconds=UPLOAD_URL_TTL_S),
        headers={"Content-Type": content_type, "If-None-Match": "*"},
    )


# ---- T9-T10a: the upload (register a signed-PUT key, or store a multipart file) ----

UPLOAD_PATH: Final = f"{API_V1}/images/upload"


def upload_payload(form: ImageUploadForm, file: bytes | None) -> bytes:
    """The bytes the request hash covers (CP2): every form field and the file part, NUL-joined;
    the multipart boundary and part order never count."""
    parts = [
        str(form.product_id).encode(),
        ImageSlot(form.slot).value.encode(),
        str(form.colourway_id or "").encode(),
        (form.storage_key or "").encode(),
    ]
    return b"\x00".join(parts) + b"\x00" + (file or b"")


def accepted_body(image_id: uuid.UUID, job_id: uuid.UUID, status: str = "queued") -> dict[str, Any]:
    return {
        "image_id": str(image_id),
        "job_id": str(job_id),
        "status": status,
        "poll_url": f"{API_V1}/jobs/{job_id}",
    }


async def enforce_live_image_cap(images: ImageRepository) -> None:
    """At most `LIVE_IMAGE_JOB_CAP` image jobs `queued|running` per tenant (PLAN D15(a))."""
    if await images.count_live_image_jobs() >= LIVE_IMAGE_JOB_CAP:
        raise LimitError("live image jobs per tenant")


async def _peek(principal: Principal, key: str, payload: bytes) -> dict[str, Any] | None:
    """A completed replay's stored body; another body or member under the key is 409."""
    digest = request_hash("POST", UPLOAD_PATH, payload)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        seen = await IdempotencyKeyRepository(uow.session, principal.tenant_id).peek(
            key, f"POST {UPLOAD_PATH}", digest, principal.user_id
        )
    if isinstance(seen, Mismatch):
        raise key_reused()
    if isinstance(seen, Replay):
        return seen.body
    return None


async def _check_target(uow: TenantUnitOfWork, form: ImageUploadForm) -> None:
    """The product (404), the colourway of that product (422) and the size-chart rule (422)."""
    await _check_product(uow.images, uow.products, form.product_id, form.slot)
    if form.colourway_id is not None:
        style = await uow.images.colourway_style(form.colourway_id)
        if style != form.product_id:
            raise InputError(ErrorCode.VALIDATION_FAILED, "colourway_id")


async def _existing(
    uow: TenantUnitOfWork, key: str, form: ImageUploadForm
) -> dict[str, Any] | None:
    """PLAN D8: a live row on the key with the same product, slot and colourway is the same
    image (its latest job is replayed); a row that differs, or a deleted one, is 422."""
    row = await uow.images.get_by_key(key)
    if row is None:
        return None
    same = (row.product_id, row.slot, row.colourway_id) == (
        form.product_id,
        ImageSlot(form.slot).value,
        form.colourway_id,
    )
    if row.deleted_at is not None or not same:
        raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
    job = await uow.job_runs.latest_for_entity("image", row.id)
    if job is None:  # a row is always written with its job; unreachable unless edited by hand
        raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
    return accepted_body(row.id, job.id, job.status)


async def _register(
    principal: Principal,
    idem_key: str,
    payload: bytes,
    form: ImageUploadForm,
    *,
    image_id: uuid.UUID,
    object_key: str,
    size: int,
) -> dict[str, Any]:
    """One `idempotent` transaction: the tenant image-job lock, then the product lock (fixed
    order), the D8 lookup, the live-job cap (429), the product re-read, the per-product cap,
    the main replacement, the row, the job and the audit rows."""
    tenant = principal.tenant_id

    async def handler(session: Any) -> tuple[int, dict[str, Any]]:
        uow = _uow_on(session, tenant)
        await uow.images.lock_tenant_image_jobs()
        await uow.images.lock_product_images(form.product_id)
        replay = await _existing(uow, object_key, form)
        if replay is not None:
            return 202, replay
        await enforce_live_image_cap(uow.images)
        await _check_target(uow, form)
        if await uow.images.count_live(form.product_id) >= MAX_LIVE_IMAGES_PER_PRODUCT:
            raise InputError(ErrorCode.VALIDATION_FAILED, "images")
        if form.slot is ImageSlot.MAIN:
            old = await uow.images.live_main(form.product_id, form.colourway_id)
            if old is not None:
                await uow.images.soft_delete(old.id)
                await _audit(
                    uow,
                    principal,
                    "image.replaced",
                    old.id,
                    before={"deleted_at": None},
                    after={"deleted_at": datetime.now(UTC).isoformat()},
                )
            position = 0
        else:
            position = await uow.images.next_position(form.product_id, form.colourway_id, form.slot)
        row = await uow.images.create(
            NewImage(
                id=image_id,
                product_id=form.product_id,
                colourway_id=form.colourway_id,
                storage_key=object_key,
                slot=form.slot,
                bytes=size,
                position=position,
            )
        )
        if row is None:  # unreachable under the product lock: the D8 lookup ran first
            raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type="image",
                entity_id=image_id,
                created_by=principal.user_id,
                payload={"image_id": str(image_id)},
                lock=IMAGE_JOB_LOCK,
            )
        )
        await _audit(
            uow,
            principal,
            "image.uploaded",
            image_id,
            after={"slot": ImageSlot(form.slot).value, "status": "uploaded"},
        )
        return 202, accepted_body(image_id, enqueued.job.id)

    _status, body = await idempotent(principal, idem_key, "POST", UPLOAD_PATH, payload, handler)
    log.info(
        "image.accepted",
        tenant_id=str(tenant),
        image_id=body.get("image_id"),
        job_id=body.get("job_id"),
    )
    return body


def _uow_on(session: Any, tenant: uuid.UUID) -> TenantUnitOfWork:
    """The repositories on the `idempotent` transaction's session (one commit for all)."""
    return repositories_on(session, tenant)


async def _audit(
    uow: TenantUnitOfWork,
    principal: Principal,
    action: str,
    image_id: uuid.UUID,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    await uow.audit.write(
        tenant_id=principal.tenant_id,
        actor_type="user",
        actor_user_id=principal.user_id,
        action=action,
        entity_type="image",
        entity_id=image_id,
        before=None if before is None else json.dumps(before),
        after=None if after is None else json.dumps(after),
    )


async def accept_image_upload(
    principal: Principal, idem_key: str, form: ImageUploadForm, file: bytes | None
) -> dict[str, Any]:
    """The `ImageAccepted` body (202). Exactly one of `form.storage_key` and `file`."""
    if (file is None) == (form.storage_key is None):
        raise InputError(ErrorCode.VALIDATION_FAILED, "file")
    payload = upload_payload(form, file)
    replayed = await _peek(principal, idem_key, payload)
    if replayed is not None:
        return replayed
    if form.storage_key is not None:
        return await _accept_key(principal, idem_key, payload, form, form.storage_key)
    assert file is not None  # noqa: S101 - narrowed by the exactly-one check above
    return await _accept_file(principal, idem_key, payload, form, file)


async def _accept_key(
    principal: Principal, idem_key: str, payload: bytes, form: ImageUploadForm, key: str
) -> dict[str, Any]:
    parsed = parse_image_key(key)
    if parsed is None:
        raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
    if parsed.tenant_id != principal.tenant_id:
        raise NotFoundError("storage_key")  # the same bytes as a key with no object
    if parsed.product_id != form.product_id:
        raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        replay = await _existing(uow, key, form)
        if replay is None:
            await _check_target(uow, form)
    if replay is not None:

        async def stored(_session: Any) -> tuple[int, dict[str, Any]]:
            return 202, replay

        _status, body = await idempotent(principal, idem_key, "POST", UPLOAD_PATH, payload, stored)
        return body
    head = await get_storage().head(images_bucket(), key)
    if head is None:
        raise NotFoundError("storage_key")
    if head.size > MAX_IMAGE_BYTES:
        raise ImageTooLargeError("image")
    if head.last_modified < datetime.now(UTC) - REGISTER_WINDOW:
        raise InputError(ErrorCode.VALIDATION_FAILED, "storage_key")
    return await _register(
        principal, idem_key, payload, form, image_id=parsed.image_id, object_key=key, size=head.size
    )


async def _accept_file(
    principal: Principal, idem_key: str, payload: bytes, form: ImageUploadForm, file: bytes
) -> dict[str, Any]:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        await _check_target(uow, form)
        if not file:
            raise InputError(ErrorCode.VALIDATION_FAILED, "file")
        if len(file) > MAX_IMAGE_BYTES:
            raise ImageTooLargeError("image")
        await enforce_live_image_cap(uow.images)  # unlocked pre-check: nothing stored past it
    image_id = uuid.uuid4()
    key = image_object_key(principal.tenant_id, form.product_id, image_id)
    # the declared part type is ignored (CP8): the job sniffs the magic bytes
    await get_storage().put(images_bucket(), key, file, MULTIPART_CONTENT_TYPE)
    return await _register(
        principal, idem_key, payload, form, image_id=image_id, object_key=key, size=len(file)
    )


# ---- T12: the report ----


async def get_report(principal: Principal, image_id: uuid.UUID) -> ReportView:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        row = await uow.images.get_live(image_id)
        if row is None:
            raise NotFoundError("image_id")
        stored = row.image_report_json or {}
        reports = {Marketplace(m): ImageReport.model_validate(r) for m, r in stored.items()}
        return ReportView(image_id=row.id, compliance_status=row.compliance_status, reports=reports)
