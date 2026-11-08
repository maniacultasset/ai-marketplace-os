"""SI12 test helpers: a product's live size-chart photo on a fake store, and its read job."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image as PILImage

from .images_support import FakeStorage, image_db_row, image_row, use_storage
from .jobs_support import Conn

READ_MODULES = ("app.workers.size_chart_read",)


def use_read_storage(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    """Point the image modules and the size-chart read worker at `store`."""
    import importlib

    use_storage(monkeypatch, store)
    for name in READ_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "get_storage", lambda: store)


def chart_photo(colour: tuple[int, int, int] = (250, 250, 250), size: int = 64) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (size, size), colour).save(out, format="JPEG")
    return out.getvalue()


def chart_image(
    conn: Conn,
    store: FakeStorage,
    tenant: uuid.UUID,
    product_id: uuid.UUID,
    *,
    derived: bool = True,
    vision: bool = True,
    **cols: Any,
) -> tuple[uuid.UUID, str | None]:
    """A live style-level `size_chart` image; with `derived`, its clean hash, its vision size in
    `metadata` and its `vision.jpg` in the store. Returns (image, clean hash)."""
    from app.services.image_keys import derived_key

    data = chart_photo()
    digest = hashlib.sha256(data + uuid.uuid4().bytes).hexdigest() if derived else None
    extra: dict[str, Any] = {}
    if derived:
        extra = {
            "clean_content_hash": digest,
            "clean_mime_type": "image/jpeg",
            "metadata": {"vision": {"w": 64, "h": 64}},
        }
    image_id = image_row(conn, tenant, product_id, slot="size_chart", **extra, **cols)
    if derived and vision:
        store.objects[derived_key(tenant, product_id, image_id, "vision.jpg")] = (
            data,
            "image/jpeg",
            datetime.now(UTC),
        )
    return image_id, digest


async def enqueue_read(tenant: uuid.UUID, image_id: uuid.UUID) -> uuid.UUID:
    """A queued size-chart read job, as the request service writes it."""
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from app.workers.policy import SIZE_CHART_READ_ENTITY, lock_for
    from mkos_schemas.enums import JobKind

    async with tenant_uow(tenant) as uow:
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type=SIZE_CHART_READ_ENTITY,
                entity_id=image_id,
                created_by=None,
                payload={"size_chart_read": True, "image_id": str(image_id)},
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, SIZE_CHART_READ_ENTITY, tenant),
            )
        )
    return enqueued.job.id


def read_state(conn: Conn, image_id: uuid.UUID) -> dict[str, Any] | None:
    metadata = image_db_row(conn, image_id)["metadata"] or {}
    state: dict[str, Any] | None = metadata.get("size_chart_read")
    return state
