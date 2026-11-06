"""Image object keys (S17b D1; spec L10193; TEAM-QUESTIONS T109): every image object sits at
`<tenant_id>/<product_id>/images/<image_id>`, three lowercase canonical UUIDs.

`parse_image_key` is the only way a client-supplied key is read: an exact full-string match of
that shape, never a string-prefix test, so a key under another tenant's prefix (or one whose
first segment merely starts like the caller's tenant id) is told apart by the parsed UUID.
Pure: no I/O.

SI1 T16: the server-only derived objects sit at `<tenant_id>/<product_id>/derived/<image_id>/<name>`
with `name` one of `clean.jpg`, `clean.png`, `vision.jpg`. `parse_derived_key` is as exact, and
neither parser ever matches the other's keys (the orphan sweep reads image keys only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

_UUID: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
IMAGE_KEY_RE: Final = re.compile(rf"\A({_UUID})/({_UUID})/images/({_UUID})\Z", re.ASCII)
DerivedName = Literal["clean.jpg", "clean.png", "vision.jpg"]
DERIVED_NAMES: Final[tuple[DerivedName, ...]] = ("clean.jpg", "clean.png", "vision.jpg")
DERIVED_KEY_RE: Final = re.compile(
    rf"\A({_UUID})/({_UUID})/derived/({_UUID})/(clean\.jpg|clean\.png|vision\.jpg)\Z", re.ASCII
)
CLEAN_NAMES: Final[dict[str, DerivedName]] = {"image/jpeg": "clean.jpg", "image/png": "clean.png"}


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


@dataclass(frozen=True, slots=True)
class DerivedKey:
    tenant_id: UUID
    product_id: UUID
    image_id: UUID
    name: DerivedName


def derived_key(tenant_id: UUID, product_id: UUID, image_id: UUID, name: DerivedName) -> str:
    for value in (tenant_id, product_id, image_id):
        if not isinstance(value, UUID):
            raise ValueError("tenant_id, product_id and image_id must be UUIDs")
    if name not in DERIVED_NAMES:
        raise ValueError("unknown derived object name")
    return f"{tenant_id}/{product_id}/derived/{image_id}/{name}"


def parse_derived_key(key: str) -> DerivedKey | None:
    """The ids and name of a well-formed derived key, else None."""
    if not isinstance(key, str):
        return None
    found = DERIVED_KEY_RE.match(key)
    if found is None:
        return None
    tenant, product, image, name = found.groups()
    return DerivedKey(UUID(tenant), UUID(product), UUID(image), name)  # type: ignore[arg-type]


# ---- SI1 T19-T22: the public copy's object name (ADR-0026) ----

PUBLIC_EXTENSIONS: Final[dict[str, str]] = {"image/jpeg": "jpg", "image/png": "png"}


def public_names(public_id: str, mime: str | None) -> list[str]:
    """The public object's name; both when the type is no longer known (a tombstone)."""
    if mime in PUBLIC_EXTENSIONS:
        return [f"{public_id}.{PUBLIC_EXTENSIONS[mime]}"]
    return [f"{public_id}.{suffix}" for suffix in PUBLIC_EXTENSIONS.values()]
