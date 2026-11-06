"""The product's images in generation (SI1 T18; PLAN D4, decision 19; CP5, CP15).

- `order_for_listing(rows)` (pure): the `CanonicalListing.images` the backend pins on every saved
  version, replacing what the pipeline returned. Order: the style's main, the style's other
  images by slot (`secondary`, `lifestyle`, `size_chart`) then position, then each colourway's
  main (colourways by position, then id), then the rest of each colourway's images. Every live
  image in a publishable slot counts, whatever its compliance status (CP5: the approve image
  gate then blocks a pending or failed one). `packaging_label` is never publishable. Cut at 20.
- `pick_context(rows)` (pure): up to 6 publishable images in the same order plus the newest
  `packaging_label`, only those with a clean derivative.
- `load_context_images(storage, tenant_id, picked)`: each picked image's `vision.jpg`, as a
  `ContextImage`. A missing or unreadable object is skipped and logged by id.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from mkos_schemas.enums import ImageSlot
from mkos_schemas.jobs import ContextImage
from mkos_schemas.listing import ImageRef
from pydantic import ValidationError

from app.adapters.storage import (
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    images_bucket,
)
from app.core.logging import get_logger
from app.repositories.images import ListingImageRow
from app.services.image_keys import derived_key

__all__ = ["load_context_images", "order_for_listing", "pick_context"]

log = get_logger(__name__)
MAX_LISTING_IMAGES: Final = 20
MAX_CONTEXT_PRODUCT_IMAGES: Final = 6
MAX_VISION_BYTES: Final = 2 * 1024 * 1024
SLOT_RANK: Final = {"main": 0, "secondary": 1, "lifestyle": 2, "size_chart": 3}
LABEL: Final = ImageSlot.PACKAGING_LABEL.value


def _key(row: ListingImageRow) -> tuple[Any, ...]:
    own = row.colourway_id is None
    colourway = (
        (0, 0, "")
        if own
        else (
            1,
            row.colourway_position if row.colourway_position is not None else 10_000,
            str(row.colourway_id),
        )
    )
    return (
        *colourway,
        SLOT_RANK.get(row.slot, 99),
        row.position,
        row.created_at,
        str(row.image_id),
    )


def _publishable(rows: Sequence[ListingImageRow]) -> list[ListingImageRow]:
    return sorted((r for r in rows if r.slot in SLOT_RANK), key=_key)


def order_for_listing(rows: Sequence[ListingImageRow]) -> list[ImageRef]:
    ordered = _publishable(rows)[:MAX_LISTING_IMAGES]
    return [
        ImageRef(image_id=row.image_id, slot=ImageSlot(row.slot), position=index)
        for index, row in enumerate(ordered)
    ]


def pick_context(rows: Sequence[ListingImageRow]) -> list[ListingImageRow]:
    derived = [r for r in rows if r.clean_content_hash is not None]
    picked = _publishable(derived)[:MAX_CONTEXT_PRODUCT_IMAGES]
    labels = sorted(
        (r for r in derived if r.slot == LABEL),
        key=lambda r: (r.created_at, str(r.image_id)),
        reverse=True,
    )
    return picked + labels[:1]


def _size(row: ListingImageRow) -> tuple[int, int] | None:
    vision = row.vision or {}
    w, h = vision.get("w"), vision.get("h")
    if isinstance(w, int) and isinstance(h, int) and not isinstance(w, bool):
        return w, h
    return None


async def load_context_images(
    storage: Any, tenant_id: UUID, picked: Sequence[ListingImageRow]
) -> list[ContextImage]:
    out: list[ContextImage] = []
    for row in picked:
        size = _size(row)
        if size is None:
            log.info("generation.context_image_skipped", image_id=str(row.image_id), reason="size")
            continue
        key = derived_key(tenant_id, row.product_id, row.image_id, "vision.jpg")
        try:
            data = await storage.get(images_bucket(), key, max_bytes=MAX_VISION_BYTES)
        except (StorageNotFound, StorageObjectTooLarge, StorageUnavailable):
            log.info("generation.context_image_skipped", image_id=str(row.image_id), reason="read")
            continue
        try:
            out.append(
                ContextImage(
                    image_id=row.image_id,
                    slot=ImageSlot(row.slot),
                    colourway_id=row.colourway_id,
                    width=size[0],
                    height=size[1],
                    data_b64=base64.b64encode(data).decode("ascii"),
                )
            )
        except ValidationError:
            log.info("generation.context_image_skipped", image_id=str(row.image_id), reason="shape")
    return out
