"""SI1 T22 (AC15; decision 21): the daily cleanup of deleted images' derived and public objects.
Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.services.image_keys import derived_key
from app.services.public_images import new_public_id

from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, image_db_row, image_row, images_db, product
from .jobs_support import JobWorld

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    import app.adapters.storage as storage_module
    from app.core.config import get_settings

    fake = FakeStorage()
    monkeypatch.setattr(storage_module, "get_storage", lambda: fake)
    monkeypatch.setenv("GCS_BUCKET_IMAGES_PUBLIC", "marketplace-images-public")
    monkeypatch.setenv("IMAGE_PUBLIC_BASE_URL", "http://localhost:9000/marketplace-images-public")
    get_settings.cache_clear()
    return fake


def _with_objects(
    jw: JobWorld, store: FakeStorage, slug: str, *, deleted: bool
) -> tuple[Any, list[str]]:
    tenant = jw.tenant(slug)
    p = product(jw.conn, tenant)
    image_id = image_row(jw.conn, tenant, p, slot="secondary")
    public_id = new_public_id()
    jw.conn.execute(
        "UPDATE images SET clean_content_hash = %s, clean_mime_type = 'image/jpeg', public_id = %s, "
        "public_written_at = now(), deleted_at = CASE WHEN %s THEN now() END WHERE id = %s",
        ["c" * 64, public_id, deleted, image_id],
    )
    keys = [derived_key(tenant, p, image_id, "clean.jpg"), f"{public_id}.jpg"]
    for key in keys:
        store.objects[key] = (b"x", "image/jpeg", datetime.now(UTC))
    return image_id, keys


async def test_deleted_row_objects_removed_and_cleared(
    images_db: JobWorld, store: FakeStorage
) -> None:
    from app.workers.periodic import sweep_deleted_image_objects

    image_id, keys = _with_objects(images_db, store, "acme", deleted=True)
    assert (await sweep_deleted_image_objects())["cleared"] >= 1
    assert not [k for k in keys if k in store.objects]
    row = image_db_row(images_db.conn, image_id)
    assert row["public_written_at"] is None and row["clean_content_hash"] is None
    assert row["public_id"] is not None  # the tombstone stays


async def test_live_row_untouched(images_db: JobWorld, store: FakeStorage) -> None:
    from app.workers.periodic import sweep_deleted_image_objects

    image_id, keys = _with_objects(images_db, store, "acme", deleted=False)
    await sweep_deleted_image_objects()
    assert all(k in store.objects for k in keys)
    assert image_db_row(images_db.conn, image_id)["public_written_at"] is not None


async def test_second_run_deletes_nothing(images_db: JobWorld, store: FakeStorage) -> None:
    from app.workers.periodic import sweep_deleted_image_objects

    _with_objects(images_db, store, "bharat", deleted=True)
    await sweep_deleted_image_objects()
    before = store.calls.count("delete")
    assert (await sweep_deleted_image_objects())["cleared"] == 0
    assert store.calls.count("delete") == before
    assert uuid is not None
