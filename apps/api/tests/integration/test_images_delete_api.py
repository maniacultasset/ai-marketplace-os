"""SI1 T21 (AC15, AC18; PLAN D7, decision 18; CP16): `DELETE /api/v1/images/{image_id}` as
app_rw, on an in-memory store. Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from app.adapters.storage import StorageUnavailable
from app.services.image_keys import derived_key, image_object_key
from app.services.public_images import new_public_id

from .conftest import LOCAL_HOSTS
from .images_support import (
    FakeStorage,
    headers,
    image_db_row,
    image_row,
    images_db,
    picture,
    product,
    register_key,
    use_storage,
)
from .jobs_support import JobWorld, as_member, bad_token, count

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["images_db"]
PREFIX = "/api/v1/images"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeStorage]:
    fake = FakeStorage()
    use_storage(monkeypatch, fake)
    monkeypatch.setenv("GCS_BUCKET_IMAGES_PUBLIC", "marketplace-images-public")
    monkeypatch.setenv("IMAGE_PUBLIC_BASE_URL", "http://localhost:9000/marketplace-images-public")
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()


def _published(jw: JobWorld, store: FakeStorage, slug: str = "acme") -> tuple[uuid.UUID, list[str]]:
    """A live image with a clean copy, a vision copy and a written public copy."""
    tenant = jw.tenant(slug)
    p = product(jw.conn, tenant)
    image_id = image_row(jw.conn, tenant, p, slot="secondary")
    jw.conn.execute(
        "UPDATE images SET clean_content_hash = %s, clean_mime_type = 'image/jpeg', "
        'public_id = %s, public_written_at = now(), metadata = \'{"vision": {"w": 1, "h": 1}}\' '
        "WHERE id = %s",
        ["a" * 64, new_public_id(), image_id],
    )
    row = image_db_row(jw.conn, image_id)
    now = datetime.now(UTC)
    keys = [
        derived_key(tenant, p, image_id, "clean.jpg"),
        derived_key(tenant, p, image_id, "vision.jpg"),
        f"{row['public_id']}.jpg",
    ]
    for key in keys:
        store.objects[key] = (b"x", "image/jpeg", now)
    store.objects[row["storage_key"]] = (b"orig", "image/jpeg", now)
    return image_id, keys


def _delete(
    api: httpx.AsyncClient, jw: JobWorld, image_id: Any, email: str = "operator@acme.test"
) -> Any:
    return api.delete(f"{PREFIX}/{image_id}", headers=as_member(jw, email))


@pytest.mark.endpoint("images_delete", case="success")
async def test_delete_removes_public_copy(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, keys = _published(images_db, store)
    response = await _delete(api, images_db, image_id)
    assert response.status_code == 204, response.text
    assert not [k for k in keys if k in store.objects]
    row = image_db_row(images_db.conn, image_id)
    assert row["deleted_at"] is not None
    assert row["storage_key"] in store.objects  # the private original stays


async def test_delete_keeps_public_id_tombstone(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, _ = _published(images_db, store)
    assert (await _delete(api, images_db, image_id)).status_code == 204
    row = image_db_row(images_db.conn, image_id)
    assert row["public_id"] is not None
    assert row["public_written_at"] is None and row["clean_content_hash"] is None
    assert "vision" not in row["metadata"]
    assert (
        count(
            images_db.conn,
            "SELECT count(*) FROM audit_log WHERE action = 'image.deleted' AND entity_id = %s",
            [image_id],
        )
        == 1
    )


async def test_second_delete_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, _ = _published(images_db, store)
    assert (await _delete(api, images_db, image_id)).status_code == 204
    assert (await _delete(api, images_db, image_id)).status_code == 404


@pytest.mark.endpoint("images_delete", case="not_found")
async def test_unknown_image_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    assert (await _delete(api, images_db, uuid.uuid4())).status_code == 404


@pytest.mark.endpoint("images_delete", case="wrong_tenant")
async def test_other_tenant_image_404(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, keys = _published(images_db, store, "bharat")
    response = await _delete(api, images_db, image_id)
    assert response.status_code == 404
    assert all(k in store.objects for k in keys)
    assert image_db_row(images_db.conn, image_id)["deleted_at"] is None


@pytest.mark.endpoint("images_delete", case="validation")
async def test_bad_id_422(images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage) -> None:
    assert (await _delete(api, images_db, "not-a-uuid")).status_code == 422


@pytest.mark.endpoint("images_delete", case="missing_auth")
async def test_missing_auth_401(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    assert (await api.delete(f"{PREFIX}/{uuid.uuid4()}")).status_code == 401


@pytest.mark.endpoint("images_delete", case="invalid_token", variant="expired")
@pytest.mark.endpoint("images_delete", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("images_delete", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token_401(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage, variant: str
) -> None:
    response = await api.delete(f"{PREFIX}/{uuid.uuid4()}", headers=bad_token(images_db, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("images_delete", case="wrong_role")
async def test_reviewer_403(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, _ = _published(images_db, store)
    assert (await _delete(api, images_db, image_id, "reviewer@acme.test")).status_code == 403


async def test_store_failure_keeps_the_marker(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    image_id, _ = _published(images_db, store)
    store.fail = {"delete": StorageUnavailable("down")}
    assert (await _delete(api, images_db, image_id)).status_code == 204
    row = image_db_row(images_db.conn, image_id)
    assert row["deleted_at"] is not None and row["public_written_at"] is not None


async def test_main_replacement_removes_the_old_copies(
    images_db: JobWorld, api: httpx.AsyncClient, store: FakeStorage
) -> None:
    tenant = images_db.tenant("acme")
    p = product(images_db.conn, tenant)
    old = image_row(images_db.conn, tenant, p, slot="main")
    images_db.conn.execute(
        "UPDATE images SET clean_content_hash = %s, clean_mime_type = 'image/png' WHERE id = %s",
        ["b" * 64, old],
    )
    clean = derived_key(tenant, p, old, "clean.png")
    store.objects[clean] = (b"x", "image/png", datetime.now(UTC))
    new_key = image_object_key(tenant, p, uuid.uuid4())
    store.objects[new_key] = (picture(64, 64), "image/jpeg", datetime.now(UTC))
    response = await register_key(
        api, headers(images_db, key=str(uuid.uuid4())), p, new_key, slot="main"
    )
    assert response.status_code == 202, response.text
    assert clean not in store.objects
    assert image_db_row(images_db.conn, old)["clean_content_hash"] is None
