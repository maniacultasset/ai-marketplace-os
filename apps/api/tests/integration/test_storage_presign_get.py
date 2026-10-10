"""S21 T2a (D9; AC8, AC14): `ObjectStorage.presign_get` against the compose MinIO. S17b already
built the capped `get(..., max_bytes=)` and the boto logger pin (its tests stay green); S21 adds
only the signed GET a publish hands the marketplace adapter."""

from __future__ import annotations

import logging
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from app.adapters.storage import (
    ObjectStorage,
    StorageObjectTooLarge,
    get_storage,
    images_bucket,
    new_storage,
)

from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStorage]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_storage.cache_clear()
    client = new_storage()
    try:
        client._client.head_bucket(Bucket=images_bucket())
    except Exception:
        pytest.fail(f"MinIO not reachable on {get_settings().s3_endpoint_url}")
    yield client
    get_storage.cache_clear()
    get_settings.cache_clear()


async def test_presign_get_no_network(
    storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("presign_get opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    url = await storage.presign_get(images_bucket(), f"{uuid.uuid4()}/x.jpg", expires_s=60)
    assert url.startswith("http") and "X-Amz-Signature=" in url


async def test_presign_get_url_fetches_object(storage: ObjectStorage) -> None:
    key = f"{uuid.uuid4()}/img.jpg"
    await storage.put(images_bucket(), key, b"\xff\xd8bytes", "image/jpeg")
    try:
        url = await storage.presign_get(images_bucket(), key, expires_s=60)
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
        assert response.status_code == 200 and response.content == b"\xff\xd8bytes"
    finally:
        await storage.delete(images_bucket(), key)


async def test_presign_get_expiry_in_query(storage: ObjectStorage) -> None:
    url = await storage.presign_get(images_bucket(), f"{uuid.uuid4()}/x.jpg", expires_s=86400)
    assert parse_qs(urlsplit(url).query)["X-Amz-Expires"] == ["86400"]
    with pytest.raises(ValueError):
        await storage.presign_get(images_bucket(), "k", expires_s=0)


async def test_storage_get_max_bytes_boundary(storage: ObjectStorage) -> None:
    key = f"{uuid.uuid4()}/cap.bin"
    await storage.put(images_bucket(), key, b"x" * 16, "application/octet-stream")
    try:
        assert await storage.get(images_bucket(), key, max_bytes=16) == b"x" * 16
        with pytest.raises(StorageObjectTooLarge):
            await storage.get(images_bucket(), key, max_bytes=15)
    finally:
        await storage.delete(images_bucket(), key)


async def test_presign_get_url_never_logged(
    storage: ObjectStorage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    url = await storage.presign_get(images_bucket(), f"{uuid.uuid4()}/x.jpg", expires_s=60)
    signature = parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]
    assert signature not in caplog.text
