"""Signed views of saved product images, so the app can show a photo after a reload.

Each live image gets a short-lived V4 signed GET (`VIEW_TTL_S`, the image-candidate preview
pattern in `image_candidates.py`) of a server-made copy, never of the seller's original upload
(which may still carry EXIF, a location included): `vision.jpg` (RGB JPEG, long side at most
1024, no metadata) when the derive wrote it, else the clean copy, else no view (the checks have
not run yet, or the photo could not be read).

The objects sit under the caller's tenant prefix in the private images bucket, so a URL opens
only one object of the tenant that asked; no public copy is ever handed out here. A signed URL is
a bearer credential: it is computed locally, never logged and never stored. When the store is
not configured the read still answers, with no view.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from app.adapters.storage import StorageConfigError, get_storage, images_bucket
from app.core.logging import get_logger
from app.repositories.images import ImageDetail, MainImageRow
from app.services.image_keys import CLEAN_NAMES, derived_key

log = get_logger(__name__)

VIEW_TTL_S: Final = 600


@dataclass(frozen=True, slots=True)
class ImageView:
    url: str
    expires_at: datetime


def view_key(
    tenant_id: UUID,
    product_id: UUID,
    image_id: UUID,
    *,
    has_vision: bool,
    clean_mime_type: str | None,
) -> str | None:
    """The derived object to show: `vision.jpg`, else the clean copy, else None."""
    if has_vision:
        return derived_key(tenant_id, product_id, image_id, "vision.jpg")
    if clean_mime_type in CLEAN_NAMES:
        return derived_key(tenant_id, product_id, image_id, CLEAN_NAMES[clean_mime_type])
    return None


async def sign_keys(keys: Mapping[UUID, str]) -> dict[UUID, ImageView]:
    """A signed GET for each key, valid `VIEW_TTL_S` seconds; `{}` when the store is not
    configured (the product read must not fail on it)."""
    if not keys:
        return {}
    try:
        storage = get_storage()
    except StorageConfigError:
        log.warning("image.view_unsigned", reason="storage_config", count=len(keys))
        return {}
    bucket = images_bucket()
    # the expiry is stated a little early, so a client refreshes before the URL stops working
    expires_at = datetime.now(UTC) + timedelta(seconds=VIEW_TTL_S - 30)
    out: dict[UUID, ImageView] = {}
    for image_id, key in keys.items():
        url = await storage.presign_get(bucket, key, expires_s=VIEW_TTL_S)
        out[image_id] = ImageView(url=url, expires_at=expires_at)
    return out


async def with_views(
    tenant_id: UUID, product_id: UUID, details: Sequence[ImageDetail]
) -> tuple[ImageDetail, ...]:
    """The details with `view_url` and `view_expires_at` set where a derived copy exists. All of
    one product's images share its `product_id` (a style's colourways included)."""
    keys: dict[UUID, str] = {}
    for detail in details:
        key = view_key(
            tenant_id,
            product_id,
            detail.image_id,
            has_vision=detail.has_vision,
            clean_mime_type=detail.clean_mime_type,
        )
        if key is not None:
            keys[detail.image_id] = key
    views = await sign_keys(keys)
    return tuple(
        (
            replace(
                detail,
                view_url=views[detail.image_id].url,
                view_expires_at=views[detail.image_id].expires_at,
            )
            if detail.image_id in views
            else detail
        )
        for detail in details
    )


def _colour_key(value: str | None) -> str | None:
    """The colourway `colour_key` form of a variant's colour (`lower(btrim(...))`)."""
    if not value:
        return None
    key = value.strip(" ").lower()
    return key or None


def pick_main(
    rows: Iterable[MainImageRow],
    *,
    product_id: UUID,
    parent_product_id: UUID | None,
    colour: str | None,
) -> MainImageRow | None:
    """The product's own main (style-level first), else, for a variant, its style's main of the
    variant's colour, else the style's style-level main. Never another colour's photo."""
    by_product: dict[UUID, list[MainImageRow]] = {}
    for row in rows:
        by_product.setdefault(row.product_id, []).append(row)
    own = by_product.get(product_id, [])
    if own:
        return own[0]
    if parent_product_id is None:
        return None
    parent = by_product.get(parent_product_id, [])
    wanted = _colour_key(colour)
    if wanted is not None:
        for row in parent:
            if row.colour_key == wanted:
                return row
    for row in parent:
        if row.colourway_id is None:
            return row
    return None


async def sign_mains(tenant_id: UUID, picks: Mapping[UUID, MainImageRow]) -> dict[UUID, ImageView]:
    """Per product id, the signed view of its picked main image."""
    keys: dict[UUID, str] = {}
    for row in picks.values():
        key = view_key(
            tenant_id,
            row.product_id,
            row.image_id,
            has_vision=row.has_vision,
            clean_mime_type=row.clean_mime_type,
        )
        if key is not None:
            keys[row.image_id] = key
    views = await sign_keys(keys)
    return {
        product_id: views[row.image_id]
        for product_id, row in picks.items()
        if row.image_id in views
    }
