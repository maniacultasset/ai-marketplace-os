"""The SI3 photo-facts test world: S18's generation world (the demo seed as `app_rw`, the stub
pipeline) with the seeded platform templates loaded (the Amazon and Flipkart kurta templates list
`neckline`, `sleeve_length` and `fit`, so a kurta product gets photo targets), and a standalone
kurta product with three product photos and a packaging label whose vision copies sit in an
in-memory storage. Rows are read back on the admin connection.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from . import factories as f
from .generation_support import GenWorld, build_gen_world, make_generating
from .images_support import FakeStorage

OPERATOR = "operator@acme.test"
REVIEWER = "reviewer@acme.test"
SKU = "KURTA-002"
PHOTO_SLOTS = ("main", "secondary", "lifestyle")


def vision_jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 80), (10, 20, 30)).save(out, format="JPEG")
    return out.getvalue()


@dataclass
class PhotoWorld:
    world: GenWorld
    store: FakeStorage
    product_id: uuid.UUID
    images: dict[str, uuid.UUID] = field(default_factory=dict)
    seen: list[Any] = field(default_factory=list)

    @property
    def conn(self) -> Any:
        return self.world.conn

    @property
    def tenant(self) -> uuid.UUID:
        return self.world.tenant("acme")

    def add_image(self, slot: str, content_hash: str | None = None) -> uuid.UUID:
        image_id = uuid.uuid4()
        reports = {
            m: {
                "image_id": str(image_id),
                "marketplace": m,
                "slot": slot,
                "pass": True,
                "processed_at": "2026-11-07T00:00:00Z",
            }
            for m in ("amazon", "flipkart")
        }
        original = vision_jpeg() + image_id.bytes  # distinct bytes per image
        storage_key = f"{self.tenant}/{self.product_id}/images/{image_id}"
        self.store.objects[storage_key] = (original, "image/jpeg", datetime.now(UTC))
        f.image(
            self.conn,
            self.tenant,
            self.product_id,
            id=image_id,
            storage_key=storage_key,
            content_hash=hashlib.sha256(original).hexdigest(),
            slot=slot,
            clean_content_hash=content_hash or uuid.uuid4().hex * 2,
            clean_mime_type="image/jpeg",
            metadata={"vision": {"w": 64, "h": 80}},
            compliance_status="passed",
            image_report_json=Jsonb(reports),
        )
        key = f"{self.tenant}/{self.product_id}/derived/{image_id}/vision.jpg"
        self.store.objects[key] = (vision_jpeg(), "image/jpeg", datetime.now(UTC))
        return image_id

    def attributes(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT attributes FROM products WHERE id = %s", [self.product_id]
        ).fetchone()
        assert row is not None
        return dict(row[0])

    def put_fact(self, key: str, fact: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE products SET attributes = attributes || jsonb_build_object(%s::text, %s::jsonb)"
            " WHERE id = %s",
            [key, Jsonb(fact), self.product_id],
        )

    def job(self, job_id: uuid.UUID) -> dict[str, Any]:
        cur = self.conn.execute("SELECT * FROM job_runs WHERE id = %s", [job_id])
        names = [d.name for d in cur.description or ()]
        row = cur.fetchone()
        assert row is not None
        return dict(zip(names, row, strict=True))

    def tasks(self) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM human_tasks WHERE product_id = %s ORDER BY created_at, id",
            [self.product_id],
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def audit(self, action: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM audit_log WHERE entity_id = %s AND action = %s ORDER BY occurred_at, id",
            [self.product_id, action],
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def photo_records(self, job_id: uuid.UUID) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM agent_runs WHERE job_id = %s AND prompt_version LIKE 'photo_facts/%%'",
            [job_id],
        )
        names = [d.name for d in cur.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def sent_set(self) -> list[list[str]]:
        rows = self.conn.execute(
            "SELECT id::text, clean_content_hash FROM images WHERE product_id = %s "
            "AND slot IN ('main', 'secondary', 'lifestyle') AND deleted_at IS NULL ORDER BY 1",
            [self.product_id],
        ).fetchall()
        return [[r[0], r[1]] for r in rows]


async def load_platform_templates() -> None:
    """`templates load` (the migrate job's default file); `auth_db` keeps ALEMBIC_DATABASE_URL on
    the admin login."""
    from app.cli import PLATFORM_TEMPLATES_PATH, run_templates_load
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert await run_templates_load(PLATFORM_TEMPLATES_PATH) == 0
    get_settings.cache_clear()


async def build_photo_world(conn: Any, monkeypatch: pytest.MonkeyPatch) -> PhotoWorld:
    from app.workers import generation

    world = await build_gen_world(conn, monkeypatch)
    await load_platform_templates()
    store = FakeStorage()
    monkeypatch.setattr(generation, "get_storage", lambda: store)
    pw = PhotoWorld(world, store, world.product("acme", SKU))
    for slot in (*PHOTO_SLOTS, "packaging_label"):
        pw.images[slot] = pw.add_image(slot)
    return pw


@pytest.fixture
async def photo_world(auth_db: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PhotoWorld]:
    from app.db.queue import get_queue_app

    get_queue_app.cache_clear()
    yield await build_photo_world(auth_db, monkeypatch)
    get_queue_app.cache_clear()


Wrapper = Callable[[Any, Any, Callable[[Any, Any], Awaitable[Any]]], Awaitable[Any]]


def spy_pipeline(
    pw: PhotoWorld, monkeypatch: pytest.MonkeyPatch, around: Wrapper | None = None
) -> None:
    """Record every context the worker sends; `around(gctx, deps, real)` may act mid-run."""
    from app.workers import generation
    from mkos_pipeline import run_listing_pipeline as real

    async def spy(gctx: Any, deps: Any) -> Any:
        pw.seen.append(gctx)
        if around is not None:
            return await around(gctx, deps, real)
        return await real(gctx, deps)

    monkeypatch.setattr(generation, "run_listing_pipeline", spy)


async def generate(
    pw: PhotoWorld, scenario: str | None, *, marketplace: str = "amazon", sku: str = SKU
) -> Any:
    """One real `listing_generate` job through the stub; returns the `ItemAccepted`."""
    from app.workers import wrapper
    from app.workers.tasks import register_bodies

    accepted = await make_generating(
        pw.world,
        sku=sku,
        instructions=None if scenario is None else f"stub:{scenario}",
        marketplace=marketplace,
    )
    register_bodies()
    await wrapper.run_job(str(accepted.job_id))
    return accepted


def regenerable(pw: PhotoWorld, listing_id: uuid.UUID) -> None:
    """Put a generated listing back to `created`, so the next `generate` on the same marketplace
    is a plain new generation (a test arrangement)."""
    from .generation_support import set_status

    set_status(pw.world, listing_id, "created")


async def seller_patch(pw: PhotoWorld, patch: dict[str, Any], email: str = OPERATOR) -> Any:
    from app.services import product_edit, products

    principal = pw.world.principal(email)
    view = await products.get_product(principal, pw.product_id)
    return await product_edit.update_product(principal, pw.product_id, patch, view.etag)


async def approve(pw: PhotoWorld, listing_id: uuid.UUID, email: str = REVIEWER) -> Any:
    """The approve service as `email`; the one item's outcome (`http_status`, `code`)."""
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    row = pw.world.listing(listing_id)
    principal = pw.world.principal(email)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(listing_id, row["current_version_id"], "approve")],
            http_key=str(uuid.uuid4()),
        )
    return outcome.items[0]


def simulate_publish_writes(pw: PhotoWorld) -> None:
    """The UPDATEs a publish makes on every image (`public_images.py`: `public_id`,
    `public_written_at`), which move `images.updated_at`."""
    from app.services.public_images import new_public_id

    ids = pw.conn.execute(
        "SELECT id FROM images WHERE product_id = %s AND deleted_at IS NULL", [pw.product_id]
    ).fetchall()
    for (image_id,) in ids:
        pw.conn.execute(
            "UPDATE images SET public_id = %s, public_written_at = now() WHERE id = %s",
            [new_public_id(), image_id],
        )


def photo_fact(value: str, image_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "value": value,
        "normalized": value,
        "source": "ai",
        "captured_via": "agent",
        "agent_run_id": str(uuid.uuid4()),
        "confidence": 0.95,
        "evidence_ref": f"image:{image_id}",
        "updated_at": "2026-11-07T00:00:00.000000Z",
        **extra,
    }


def seller_fact(value: Any) -> dict[str, Any]:
    return {
        "value": value,
        "source": "seller",
        "captured_via": "api",
        "updated_at": "2026-11-07T00:00:00.000000Z",
    }
