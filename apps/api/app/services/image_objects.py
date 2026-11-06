"""Removing an image's derived and public objects (SI1 T21, T22; PLAN D6 removal, D7; CP9, CP16).

- `delete_image_objects(storage, tenant_id, row)`: the derived objects (`clean.jpg`, `clean.png`,
  `vision.jpg`) in the private bucket and, with a `public_id`, the public copy. A missing object
  counts as deleted. True when all are gone; a store failure is False (the daily step retries).
  The private original is not touched (it has its own lifecycle).
- The tenant purge step (T22, CP9) is `app.db.image_purge.purge_public_images` (raw SQL as the
  migrator).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.adapters.storage import StorageUnavailable, images_bucket, images_public_bucket
from app.core.logging import get_logger
from app.services.image_keys import DERIVED_NAMES, derived_key, parse_image_key, public_names

__all__ = ["delete_image_objects", "public_names"]

log = get_logger(__name__)


async def delete_image_objects(storage: Any, tenant_id: uuid.UUID, row: Any) -> bool:
    """`row` carries `id`, `storage_key`, `public_id` and `clean_mime_type`."""
    parsed = parse_image_key(row.storage_key)
    try:
        if parsed is not None and parsed.tenant_id == tenant_id:
            for name in DERIVED_NAMES:
                await storage.delete(
                    images_bucket(), derived_key(tenant_id, parsed.product_id, row.id, name)
                )
        if row.public_id is not None:
            bucket = images_public_bucket()
            if bucket is None:  # hosting switched off: the copy cannot be reached to delete
                log.warning("image.public_delete_unconfigured", image_id=str(row.id))
                return False
            for public_name in public_names(row.public_id, row.clean_mime_type):
                await storage.delete(bucket, public_name)
    except StorageUnavailable:
        log.warning("image.objects_delete_failed", image_id=str(row.id))
        return False
    return True
