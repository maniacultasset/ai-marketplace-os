"""Public image copies at publish (SI1 T19; ADR-0026; PLAN D6, decisions 6, 7; CP1, CP16).

`ensure_public(tenant_id, image)` returns the stable URL a marketplace fetches,
`IMAGE_PUBLIC_BASE_URL/<public_id>.<jpg|png>`, for one image of the approved version. It runs in
the publish worker after the pins and before the write-ahead, with no transaction open during
storage I/O:

1. No clean copy yet → refused (`IMAGE_NON_COMPLIANT` "The image is not prepared yet.").
2. The clean copy is read from the private bucket (10 MiB cap) and must hash to
   `clean_content_hash`, else refused and the listing revalidated.
3. No `public_id` → one short transaction names it (a live row only; a clash with another row's
   name retries once with a new name; a lost race reads the stored name; a deleted row is
   refused "The image was deleted." and nothing is written).
4. `public_written_at` set → the URL. Else HEAD the public object, PUT it when absent (type,
   `Cache-Control`, no metadata), then mark it written. If the row turned out deleted meanwhile
   (CP16), the object just written is deleted again, the mark cleared and the item refused; a
   failed delete is retried by the daily sweep.
5. `StorageUnavailable` → `RetryableJobError` (nothing was sent yet).

`public_id` is 26 lowercase base32 characters from 16 random bytes: it shares nothing with the
tenant, product or image ids.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
    images_public_bucket,
)
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.repositories.uow import tenant_uow
from app.services.image_keys import (
    CLEAN_NAMES,
    PUBLIC_EXTENSIONS,
    derived_key,
    parse_image_key,
)
from app.services.publish_outcome import refusal_report
from app.workers.errors import JobFailed, RetryableJobError

__all__ = [
    "PUBLIC_CACHE_CONTROL",
    "PublicImage",
    "ensure_public",
    "hosting_configured",
    "new_public_id",
    "public_object_name",
    "public_url",
]

log = get_logger(__name__)
CLEAN_MAX_BYTES: Final = 10 * 1024 * 1024
# V1 hardening #17: one day everywhere (browser and edge), never `immutable`, so a deleted or
# replaced image is gone from every cache within a day, as the privacy policy says; it matches
# the images edge worker's `cacheTtl` (86400, scripts/deploy/cloudflare/images-edge-worker.js).
PUBLIC_CACHE_CONTROL: Final = "public, max-age=86400"
EXTENSIONS: Final = PUBLIC_EXTENSIONS
PUBLIC_ID_UNIQUE: Final = "images_public_id_uq"


@dataclass(frozen=True, slots=True)
class PublicImage:
    """The image row fields a public copy needs (the publish worker's `ImageCheck`)."""

    id: uuid.UUID
    index: int
    storage_key: str | None
    clean_content_hash: str | None
    clean_mime_type: str | None
    public_id: str | None
    public_written_at: Any | None


def new_public_id() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def public_object_name(public_id: str, mime: str) -> str:
    return f"{public_id}.{EXTENSIONS[mime]}"


def public_url(base: str, public_id: str, mime: str) -> str:
    return f"{base}/{public_object_name(public_id, mime)}"


def hosting_configured() -> bool:
    settings = get_settings()
    return (
        settings.gcs_bucket_images_public is not None and settings.image_public_base_url is not None
    )


def _refused(image: PublicImage, message: str, persist: Any = None) -> JobFailed:
    return JobFailed(
        refusal_report(ErrorCode.IMAGE_NON_COMPLIANT, f"images[{image.index}]", message),
        persist=persist,
    )


async def _name(tenant_id: uuid.UUID, image: PublicImage) -> str:
    for _ in range(2):
        try:
            async with tenant_uow(tenant_id) as uow:
                stored = await uow.images.set_public_id(image.id, new_public_id())
        except ConflictError as exc:
            if exc.constraint != PUBLIC_ID_UNIQUE:
                raise
            continue  # another row holds the name: one retry with a new one
        if stored is None:
            raise _refused(image, "The image was deleted.")
        return stored
    raise RetryableJobError(ErrorCode.INTERNAL_ERROR)


async def ensure_public(
    tenant_id: uuid.UUID,
    image: PublicImage,
    *,
    persist: Any = None,
) -> str:
    """The image's public URL (module docstring). `persist` revalidates the listing when the
    prepared copy no longer matches its checked hash."""
    settings = get_settings()
    base, bucket = settings.image_public_base_url, images_public_bucket()
    if base is None or bucket is None:  # the caller checks first (CP1); never a signed URL
        raise JobFailed(
            refusal_report(ErrorCode.INTERNAL_ERROR, "listing", "image hosting is not configured")
        )
    mime = image.clean_mime_type
    parsed = None if image.storage_key is None else parse_image_key(image.storage_key)
    if image.clean_content_hash is None or mime not in EXTENSIONS or parsed is None:
        raise _refused(image, "The image is not prepared yet.")
    storage = get_storage()
    clean_key = derived_key(tenant_id, parsed.product_id, image.id, CLEAN_NAMES[mime])
    try:
        data = await storage.get(images_bucket(), clean_key, max_bytes=CLEAN_MAX_BYTES)
    except (StorageNotFound, StorageObjectTooLarge):
        raise _refused(image, "The prepared image is missing.", persist) from None
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    if hashlib.sha256(data).hexdigest() != image.clean_content_hash:
        raise _refused(image, "The prepared image changed after it was checked.", persist)
    public_id = image.public_id or await _name(tenant_id, image)
    url = public_url(base, public_id, mime)
    if image.public_written_at is not None:
        return url
    name = public_object_name(public_id, mime)
    try:
        if await storage.head(bucket, name) is None:
            await storage.put(bucket, name, data, mime, cache_control=PUBLIC_CACHE_CONTROL)
    except StorageUnavailable:
        raise RetryableJobError(ErrorCode.INTERNAL_ERROR) from None
    async with tenant_uow(tenant_id) as uow:
        deleted = await uow.images.mark_public_written(image.id, public_id)
    if deleted is None or deleted:
        # CP16: the image was deleted while its copy was being written: take the copy down
        try:
            await storage.delete(bucket, name)
        except StorageUnavailable:
            log.warning("image.public_delete_failed", image_id=str(image.id))
        else:
            async with tenant_uow(tenant_id) as uow:
                await uow.images.clear_public_written(image.id)
        raise _refused(image, "The image was deleted.")
    log.info("image.public_written", image_id=str(image.id))
    return url
