"""The tenant purge step for public image copies (SI1 T22, CP9; ADR-0026).

Public names carry no tenant prefix, so a purge that drops the `images` rows first loses them.
`purge_public_images(connection, storage, tenant_id)` runs on the caller's connection and role
(the migrator under `seed --reset`, or `images unpublish --tenant` before a by-hand
`purge_tenant`), so RLS on an offboarded tenant is not in the way. It selects every row of the
tenant with a `public_id` (live, deleted or a tombstone), deletes each public object (a missing
one counts as deleted), then NULLs `public_id` and `public_written_at` in batches. A store
failure stops the run with the count so far: the names left keep the 0015 delete guard refusing
the purge, so a retry is safe.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import text

from app.adapters.storage import StorageUnavailable, images_public_bucket
from app.core.logging import get_logger
from app.services.image_keys import public_names

__all__ = ["public_names_left", "purge_public_images"]

log = get_logger(__name__)
PURGE_BATCH: Final = 500
_PUBLIC_ROWS: Final = text(
    "SELECT id, public_id, clean_mime_type FROM public.images "
    "WHERE tenant_id = :t AND public_id IS NOT NULL ORDER BY id"
)
_UNNAME: Final = text(
    "UPDATE public.images SET public_id = NULL, public_written_at = NULL "
    "WHERE tenant_id = :t AND id = ANY(:ids)"
)
_LEFT: Final = text(
    "SELECT count(*) FROM public.images WHERE tenant_id = :t AND public_id IS NOT NULL"
)


async def purge_public_images(connection: Any, storage: Any, tenant_id: uuid.UUID) -> int:
    """Returns the rows unnamed (module docstring)."""
    rows = (await connection.execute(_PUBLIC_ROWS, {"t": tenant_id})).all()
    if not rows:
        return 0
    bucket = images_public_bucket()
    if bucket is None:
        log.warning("image.purge_public_unconfigured", tenant_id=str(tenant_id), rows=len(rows))
        return 0
    done: list[uuid.UUID] = []
    try:
        for image_id, public_id, mime in rows:
            for name in public_names(public_id, mime):
                await storage.delete(bucket, name)
            done.append(image_id)
    except StorageUnavailable:
        log.warning("image.purge_public_stopped", tenant_id=str(tenant_id), done=len(done))
    for start in range(0, len(done), PURGE_BATCH):
        await connection.execute(
            _UNNAME, {"t": tenant_id, "ids": done[start : start + PURGE_BATCH]}
        )
    return len(done)


async def public_names_left(connection: Any, tenant_id: uuid.UUID) -> int:
    return int((await connection.execute(_LEFT, {"t": tenant_id})).scalar_one())
