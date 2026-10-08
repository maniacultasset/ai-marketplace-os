"""Image object keys (S17b D1; spec L10193; TEAM-QUESTIONS T109): every image object sits at
`<tenant_id>/<product_id>/images/<image_id>`, three lowercase canonical UUIDs.

`parse_image_key` is the only way a client-supplied key is read: an exact full-string match of
that shape, never a string-prefix test, so a key under another tenant's prefix (or one whose
first segment merely starts like the caller's tenant id) is told apart by the parsed UUID.
Pure: no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from uuid import UUID

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
IMAGE_KEY_RE: Final = re.compile(rf"\A({_UUID})/({_UUID})/images/({_UUID})\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class ImageKey:
    tenant_id: UUID
    product_id: UUID
    image_id: UUID


def image_object_key(tenant_id: UUID, product_id: UUID, image_id: UUID) -> str:
    for value in (tenant_id, product_id, image_id):
        if not isinstance(value, UUID):
            raise ValueError("tenant_id, product_id and image_id must be UUIDs")
    return f"{tenant_id}/{product_id}/images/{image_id}"


def parse_image_key(key: str) -> ImageKey | None:
    """The three ids of a well-formed image key, else None."""
    if not isinstance(key, str):
        return None
    found = IMAGE_KEY_RE.match(key)
    if found is None:
        return None
    tenant, product, image = (UUID(part) for part in found.groups())
    return ImageKey(tenant_id=tenant, product_id=product, image_id=image)
