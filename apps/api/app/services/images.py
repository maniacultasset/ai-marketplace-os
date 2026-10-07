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

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, Marketplace
from mkos_schemas.images import ImageReport

from app.adapters.storage import get_storage, images_bucket
from app.core.errors import DomainError, InputError, NotFoundError
from app.core.logging import get_logger
from app.core.rbac import Principal
from app.repositories.images import ImageRepository
from app.repositories.uow import tenant_uow
from app.services.image_keys import image_object_key

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
