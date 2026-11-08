"""Shared helpers for the SI7 candidate suites: a source main photo with its clean copy in the
fake store, a size-chart product, and a candidate enqueued the way the create service does."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from . import factories as f
from .images_support import FakeStorage, image_row, product, use_storage
from .jobs_support import Conn

PRODUCT_RGB = (40, 70, 160)
CANDIDATE_MODULES = ("app.workers.image_candidate", "app.services.image_candidates")
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 101, "length": 72}]}


def use_candidate_storage(monkeypatch: pytest.MonkeyPatch, store: Any) -> None:
    """Point the image modules and the candidate modules at `store`."""
    import importlib

    use_storage(monkeypatch, store)
    for name in CANDIDATE_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "get_storage", lambda: store)


def photo(
    size: tuple[int, int] = (1500, 2000),
    bg: tuple[int, int, int] = (245, 245, 245),
    fmt: str = "PNG",
) -> bytes:
    img = PILImage.new("RGB", size, bg)
    w, h = size
    ImageDraw.Draw(img).rectangle((w // 4, h // 4, 3 * w // 4, 3 * h // 4), fill=PRODUCT_RGB)
    out = io.BytesIO()
    img.save(out, format=fmt)
    return out.getvalue()


def source_main(
    conn: Conn,
    store: FakeStorage,
    tenant: uuid.UUID,
    *,
    product_id: uuid.UUID | None = None,
    clean: bytes | None = None,
    **cols: Any,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A product's live main with a stored clean PNG copy; returns (product, image, hash)."""
    from app.services.image_keys import derived_key

    p = product_id or product(conn, tenant)
    data = clean if clean is not None else photo()
    digest = hashlib.sha256(data).hexdigest()
    image_id = image_row(
        conn,
        tenant,
        p,
        slot="main",
        clean_content_hash=digest,
        clean_mime_type="image/png",
        **cols,
    )
    store.objects[derived_key(tenant, p, image_id, "clean.png")] = (
        data,
        "image/png",
        datetime.now(UTC),
    )
    vision = PILImage.open(io.BytesIO(data)).convert("RGB")
    vision.thumbnail((1024, 1024))
    out = io.BytesIO()
    vision.save(out, format="JPEG", quality=85)
    store.objects[derived_key(tenant, p, image_id, "vision.jpg")] = (
        out.getvalue(),
        "image/jpeg",
        datetime.now(UTC),
    )
    return p, image_id, digest


@pytest.fixture
def edit_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The AI edit switched on for the test (the local direct gateway: the stub pipeline)."""
    from app.core.config import get_settings

    monkeypatch.setenv("IMAGE_EDIT_ENABLED", "true")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def chart_product(conn: Conn, tenant: uuid.UUID, chart: dict[str, Any] | None = None) -> uuid.UUID:
    attributes = {
        "size_measurements": {
            "normalized": chart or CHART,
            "source": "seller",
            "captured_via": "api",
            "updated_at": "2026-11-06T00:00:00Z",
        }
    }
    return product(conn, tenant, attributes=attributes)


async def enqueue_candidate(
    tenant: uuid.UUID,
    product_id: uuid.UUID,
    source_image_id: uuid.UUID | None,
    source_hash: str,
    *,
    kind: str = "fix",
    generator_key: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A queued candidate and its job, as the create service writes them: (candidate, job)."""
    from app.repositories.image_candidates import NewCandidate
    from app.repositories.jobs import EnqueueSpec
    from app.repositories.uow import tenant_uow
    from app.services.image_candidates import (
        FIX_GENERATOR,
        SIZE_CHART_GENERATOR,
        edit_generator_key,
    )
    from app.workers.policy import lock_for
    from mkos_schemas.enums import JobKind

    key = (
        generator_key
        or {
            "fix": FIX_GENERATOR,
            "size_chart": SIZE_CHART_GENERATOR,
            "white_background": edit_generator_key("google/gemini-2.5-flash-image"),
        }[kind]
    )
    async with tenant_uow(tenant) as uow:
        row, created = await uow.image_candidates.create_or_get(
            NewCandidate(
                id=uuid.uuid4(),
                product_id=product_id,
                colourway_id=None,
                source_image_id=source_image_id,
                kind=kind,
                source_hash=source_hash,
                generator_key=key,
                created_by=None,
            )
        )
        assert created
        enqueued = await uow.job_runs.enqueue(
            EnqueueSpec(
                kind=JobKind.IMAGE_COMPLIANCE,
                entity_type="image_candidate",
                entity_id=row.id,
                created_by=None,
                payload={"candidate_id": str(row.id)},
                max_attempts=1 if kind == "white_background" else None,
                lock=lock_for(JobKind.IMAGE_COMPLIANCE, "image_candidate", tenant),
            )
        )
        assert await uow.image_candidates.set_job(row.id, enqueued.job.id)
    return row.id, enqueued.job.id


def candidate_row(conn: Conn, candidate_id: uuid.UUID) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM image_candidates WHERE id = %s", [candidate_id])
    names = [d.name for d in cur.description or ()]
    found = cur.fetchone()
    assert found is not None
    return dict(zip(names, found, strict=True))


def agent_run_count(conn: Conn, tenant: uuid.UUID) -> int:
    row = conn.execute("SELECT count(*) FROM agent_runs WHERE tenant_id = %s", [tenant]).fetchone()
    assert row is not None
    return int(row[0])


__all__ = ["f"]
