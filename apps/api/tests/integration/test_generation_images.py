"""SI1 T18 (AC10, AC11; CP5, CP15): the product's images reach the pipeline as `ContextImage`s
and the saved version lists them, pinned before the Rules Engine re-check. Run in the Docker
batch (`make api-tests-integration`)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating
from .images_support import FakeStorage

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _vision() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 80), (10, 20, 30)).save(out, format="JPEG")
    return out.getvalue()


def _image(world: GenWorld, product_id: uuid.UUID, slot: str, **cols: Any) -> uuid.UUID:
    acme = world.tenant("acme")
    image_id = uuid.uuid4()
    f.image(
        world.conn,
        acme,
        product_id,
        id=image_id,
        storage_key=f"{acme}/{product_id}/images/{image_id}",
        slot=slot,
        clean_content_hash="a" * 64,
        clean_mime_type="image/jpeg",
        metadata={"vision": {"w": 64, "h": 80}},
        **cols,
    )
    return image_id


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    from app.workers import generation

    fake = FakeStorage()
    monkeypatch.setattr(generation, "get_storage", lambda: fake)
    return fake


def _put_vision(
    store: FakeStorage, world: GenWorld, product_id: uuid.UUID, image_id: uuid.UUID
) -> None:
    key = f"{world.tenant('acme')}/{product_id}/derived/{image_id}/vision.jpg"
    store.objects[key] = (_vision(), "image/jpeg", datetime.now(UTC))


async def _run(world: GenWorld, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Any]]:
    from app.workers import generation, wrapper
    from app.workers.tasks import register_bodies

    seen: list[Any] = []
    from mkos_pipeline import run_listing_pipeline as real

    async def spy(gctx: Any, deps: Any) -> Any:
        seen.append(gctx)
        return await real(gctx, deps)

    monkeypatch.setattr(generation, "run_listing_pipeline", spy)
    accepted = await make_generating(world)
    register_bodies()
    await wrapper.run_job(str(accepted.job_id))
    return accepted, seen


def _version_images(world: GenWorld, job_id: uuid.UUID) -> list[dict[str, Any]]:
    row = world.conn.execute(
        "SELECT content_json FROM listing_versions WHERE job_id = %s", [job_id]
    ).fetchone()
    assert row is not None, "no version saved"
    content: Any = row[0]
    images: list[dict[str, Any]] = content["images"]
    return images


async def test_context_images_and_version_images(
    gen_world: GenWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = gen_world.product("acme", "KURTA-002")
    main = _image(gen_world, product_id, "main")
    second = _image(gen_world, product_id, "secondary")
    life = _image(gen_world, product_id, "lifestyle")
    label = _image(gen_world, product_id, "packaging_label")
    for image_id in (main, second, life, label):
        _put_vision(store, gen_world, product_id, image_id)
    accepted, seen = await _run(gen_world, monkeypatch)
    assert seen and [i.image_id for i in seen[0].images] == [main, second, life, label]
    assert all(i.width <= 1024 and i.height <= 1024 for i in seen[0].images)
    images = _version_images(gen_world, accepted.job_id)
    assert [i["image_id"] for i in images] == [str(main), str(second), str(life)]


async def test_missing_vision_is_skipped_but_listed(
    gen_world: GenWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = gen_world.product("acme", "KURTA-002")
    main = _image(gen_world, product_id, "main")
    second = _image(gen_world, product_id, "secondary")
    _put_vision(store, gen_world, product_id, main)
    accepted, seen = await _run(gen_world, monkeypatch)
    assert [i.image_id for i in seen[0].images] == [main]
    images = _version_images(gen_world, accepted.job_id)
    assert [i["image_id"] for i in images] == [str(main), str(second)]
