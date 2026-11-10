"""Queue the photo read on upload (V1.1, client input 8 Nov 2026: read the photo details as soon as
a front, back or close-up photo is uploaded on an existing product).

`queue_photo_read(uow, image_id)` runs in the upload checks' own unit of work, right after the
checks stored the photo's vision copy (`app.services.image_compliance`). It queues at most one
`image_compliance` job about a `photo_facts_read` (entity = the product), and only when:

- `PHOTO_READ_ON_UPLOAD_ENABLED` is on;
- the photo is live, the front (`main`) or a `secondary` with role `back` or `close_up`, and has
  its clean copy (a corrupt photo has none);
- its product is a style or a standalone (a variant's details belong to its style) with a
  category;
- the product's photo set (the `(image_id, clean_content_hash)` pairs generation would read) is
  not the newest one recorded: a set already read is never paid for again.

The job key is a digest of the product and that photo set, so a second photo check of the same
set (a re-check, a retried upload job) finds the first job and queues nothing. The job takes the
per-tenant AI lock, so one AI image call runs per brand; when several photos land together, the
first job to run reads the set as it is then, and the later ones end `unchanged` with no call.
Nothing here calls a model or writes a fact.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Final

from mkos_schemas.enums import JobKind

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.images import ListingImageRow
from app.repositories.jobs import EnqueueSpec
from app.repositories.uow import TenantUnitOfWork
from app.services.generation_images import pick_context
from app.services.photo_facts import PHOTO_SLOTS, PhotoSet
from app.workers.policy import PHOTO_FACTS_READ_ENTITY, lock_for

__all__ = ["TRIGGER_ROLES", "job_key", "queue_photo_read", "triggers_read", "upload_photo_set"]

log = get_logger(__name__)
TRIGGER_ROLES: Final = frozenset({"back", "close_up"})
KEY_VERSION: Final = "photo_facts_read:v1"


def triggers_read(slot: str, role: str | None) -> bool:
    """A front, back or close-up photo queues a read; a lifestyle, plain secondary, size chart or
    packaging label never does."""
    return slot == "main" or (slot == "secondary" and role in TRIGGER_ROLES)


def _has_vision(row: ListingImageRow) -> bool:
    vision = row.vision or {}
    w, h = vision.get("w"), vision.get("h")
    return isinstance(w, int) and isinstance(h, int) and not isinstance(w, bool)


def upload_photo_set(rows: list[ListingImageRow]) -> PhotoSet:
    """The photo set generation would read from these rows (`pick_context`, product photos with a
    vision copy), as sorted `(image_id, clean_content_hash)` pairs."""
    return tuple(
        sorted(
            (str(row.image_id), row.clean_content_hash or "")
            for row in pick_context(rows)
            if row.slot in PHOTO_SLOTS and _has_vision(row)
        )
    )


def job_key(product_id: uuid.UUID, photos: PhotoSet) -> str:
    """64 hex: one read job per product and photo set."""
    body = "\n".join([KEY_VERSION, str(product_id), *(f"{i}:{h}" for i, h in photos)])
    return hashlib.sha256(body.encode()).hexdigest()


async def queue_photo_read(uow: TenantUnitOfWork, image_id: uuid.UUID) -> uuid.UUID | None:
    """The queued (or already queued) read job's id, or None when no read is due."""
    if not get_settings().photo_read_on_upload_enabled:
        return None
    image = await uow.images.get_live(image_id)
    if image is None or image.clean_content_hash is None:
        return None
    if not triggers_read(image.slot, image.role):
        return None
    product = await uow.products.get_by_id(image.product_id)
    if product is None or product.parent_product_id is not None or product.category is None:
        return None
    photos = upload_photo_set(list(await uow.images.listing_set(product.id)))
    if not photos or photos == await uow.job_runs.latest_photo_set(product.id):
        return None
    tenant = uow.products.tenant_id
    enqueued = await uow.job_runs.enqueue(
        EnqueueSpec(
            kind=JobKind.IMAGE_COMPLIANCE,
            entity_type=PHOTO_FACTS_READ_ENTITY,
            entity_id=product.id,
            created_by=None,
            payload={"photo_facts_read": True, "product_id": str(product.id)},
            lock=lock_for(JobKind.IMAGE_COMPLIANCE, PHOTO_FACTS_READ_ENTITY, tenant),
            job_key=job_key(product.id, photos),
        )
    )
    log.info(
        "photo_read.queued",
        product_id=str(product.id),
        image_id=str(image_id),
        job_id=str(enqueued.job.id),
        created=enqueued.created,
    )
    return enqueued.job.id
