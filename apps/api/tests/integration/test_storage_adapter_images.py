"""S17b T2 (AC6, AC12; PLAN D9): the image calls of the storage adapter against the compose
MinIO: the create-only signed PUT, `head`, the capped `get`, `delete`, `delete_many` and the
paged `iter_older_than`. An unreachable MinIO fails these tests, never skips them."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.adapters import storage as storage_module
from app.adapters.storage import (
    ObjectStorage,
    StorageNotFound,
    StorageObjectTooLarge,
    StorageUnavailable,
    get_storage,
    images_bucket,
    new_storage,
)

from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
JPEG = "image/jpeg"


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


def _prefix() -> str:
    return f"{uuid.uuid4()}/"


async def _signed(storage: ObjectStorage, key: str, length: int, expires_s: int = 60) -> str:
    return await storage.presign_put(
        images_bucket(), key, content_type=JPEG, content_length=length, expires_s=expires_s
    )


def _put(url: str, data: bytes, **headers: str) -> httpx.Response:
    base = {"Content-Type": JPEG, "If-None-Match": "*"}
    base.update(headers)
    return httpx.put(url, content=data, headers={k: v for k, v in base.items() if v})


async def test_presigned_put_accepts_exact_length_and_type(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    url = await _signed(storage, key, 5)
    assert _put(url, b"12345").status_code == 200
    head = await storage.head(images_bucket(), key)
    assert head is not None and head.size == 5 and head.content_type == JPEG


async def test_presigned_put_other_length_refused(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    url = await _signed(storage, key, 5)
    assert _put(url, b"123456").status_code == 403
    assert await storage.head(images_bucket(), key) is None


async def test_presigned_put_other_type_refused(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    url = await _signed(storage, key, 5)
    assert _put(url, b"12345", **{"Content-Type": "image/png"}).status_code == 403
    assert await storage.head(images_bucket(), key) is None


async def test_presigned_put_expired_refused(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    url = await _signed(storage, key, 5, expires_s=1)
    await asyncio.sleep(2.1)
    assert _put(url, b"12345").status_code == 403
    assert await storage.head(images_bucket(), key) is None


async def test_presigned_put_cannot_overwrite(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    url = await _signed(storage, key, 5)
    first = _put(url, b"12345")
    assert first.status_code == 200
    assert _put(url, b"54321").status_code == 412
    # the header is signed: leaving it out is refused too (MinIO answers 400 AccessDenied)
    assert _put(url, b"54321", **{"If-None-Match": ""}).status_code in (400, 403)
    assert await storage.get(images_bucket(), key) == b"12345"
    head = storage._client.head_object(Bucket=images_bucket(), Key=key)
    assert head["ETag"] == first.headers["ETag"]


async def test_head_missing_none(storage: ObjectStorage) -> None:
    assert await storage.head(images_bucket(), _prefix() + "nothing") is None


async def test_head_reports_size_type_time(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    before = datetime.now(UTC) - timedelta(seconds=5)
    await storage.put(images_bucket(), key, b"x" * 42, "application/octet-stream")
    head = await storage.head(images_bucket(), key)
    assert head is not None
    assert head.size == 42
    assert head.content_type == "application/octet-stream"
    assert before <= head.last_modified <= datetime.now(UTC) + timedelta(seconds=5)


async def test_get_over_cap_raises_reads_cap_plus_one(
    storage: ObjectStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _prefix() + "a"
    await storage.put(images_bucket(), key, b"x" * 100, JPEG)
    sizes: list[Any] = []
    real = storage._client.get_object

    def spy(**kwargs: Any) -> Any:
        response = real(**kwargs)
        body = response["Body"]
        original = body.read

        def read(amt: Any = None) -> bytes:
            sizes.append(amt)
            return original(amt)  # type: ignore[no-any-return]

        body.read = read
        return response

    monkeypatch.setattr(storage._client, "get_object", spy)
    with pytest.raises(StorageObjectTooLarge):
        await storage.get(images_bucket(), key, max_bytes=10)
    assert sizes == [11]
    assert await storage.get(images_bucket(), key, max_bytes=100) == b"x" * 100


async def test_get_without_max_bytes_still_reads(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    await storage.put(images_bucket(), key, b"y" * 2048, JPEG)
    assert await storage.get(images_bucket(), key) == b"y" * 2048
    with pytest.raises(StorageNotFound):
        await storage.get(images_bucket(), key + "missing", max_bytes=5)


async def _collect(storage: ObjectStorage, prefix: str, **kwargs: Any) -> list[list[str]]:
    older = kwargs.pop("older_than", datetime.now(UTC) + timedelta(minutes=1))
    pages: list[list[str]] = []
    async for page in storage.iter_older_than(images_bucket(), prefix, older, **kwargs):
        pages.append([o.key for o in page])
    return pages


async def test_iter_older_than_prefix_bound(storage: ObjectStorage) -> None:
    root = str(uuid.uuid4())
    await storage.put(images_bucket(), f"{root}a/x", b"1", JPEG)
    await storage.put(images_bucket(), f"{root}ab/x", b"1", JPEG)
    pages = await _collect(storage, f"{root}a/")
    assert pages == [[f"{root}a/x"]]


async def test_iter_older_than_respects_age(storage: ObjectStorage) -> None:
    prefix = _prefix()
    await storage.put(images_bucket(), prefix + "young", b"1", JPEG)
    old = datetime.now(UTC) - timedelta(hours=1)
    assert await _collect(storage, prefix, older_than=old) == [[]]


async def _many(storage: ObjectStorage, prefix: str, n: int) -> list[str]:
    keys = [f"{prefix}{i:05d}" for i in range(n)]
    sem = asyncio.Semaphore(32)

    async def one(k: str) -> None:
        async with sem:
            await storage.put(images_bucket(), k, b"1", JPEG)

    await asyncio.gather(*(one(k) for k in keys))
    return keys


async def test_iter_older_than_pages_past_1000(storage: ObjectStorage) -> None:
    prefix = _prefix()
    keys = await _many(storage, prefix, 2500)
    pages = await _collect(storage, prefix, page_size=1000)
    assert [len(p) for p in pages] == [1000, 1000, 500]
    assert sorted(k for p in pages for k in p) == keys


async def test_iter_older_than_start_after(storage: ObjectStorage) -> None:
    prefix = _prefix()
    keys = await _many(storage, prefix, 10)
    pages = await _collect(storage, prefix, start_after=keys[6])
    assert [k for p in pages for k in p] == keys[7:]


async def test_list_prefix_without_slash_raises(storage: ObjectStorage) -> None:
    with pytest.raises(ValueError, match="end with"):
        await _collect(storage, "no-slash")
    with pytest.raises(ValueError, match="under prefix"):
        await _collect(storage, "a/", start_after="b/x")


async def test_delete_missing_ok(storage: ObjectStorage) -> None:
    key = _prefix() + "a"
    await storage.delete(images_bucket(), key)
    await storage.put(images_bucket(), key, b"1", JPEG)
    await storage.delete(images_bucket(), key)
    assert await storage.head(images_bucket(), key) is None


async def test_delete_many_removes_all_and_caps_1000(storage: ObjectStorage) -> None:
    prefix = _prefix()
    keys = await _many(storage, prefix, 30)
    await storage.delete_many(images_bucket(), keys)
    assert await _collect(storage, prefix) == [[]]
    with pytest.raises(ValueError):
        await storage.delete_many(images_bucket(), [f"{prefix}{i}" for i in range(1001)])
    with pytest.raises(ValueError):
        await storage.delete_many(images_bucket(), [])


@pytest.fixture
async def dead_store(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ObjectStorage]:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    from app.core.config import get_settings

    monkeypatch.setenv("S3_ENDPOINT_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(storage_module, "CONNECT_TIMEOUT_S", 1)
    get_settings.cache_clear()
    yield new_storage()
    get_settings.cache_clear()


async def test_storage_unavailable_mapped(dead_store: ObjectStorage) -> None:
    key = _prefix() + "a"
    with pytest.raises(StorageUnavailable):
        await dead_store.head(images_bucket(), key)
    with pytest.raises(StorageUnavailable):
        await dead_store.get(images_bucket(), key, max_bytes=10)
    with pytest.raises(StorageUnavailable):
        await dead_store.delete(images_bucket(), key)
    with pytest.raises(StorageUnavailable):
        await dead_store.delete_many(images_bucket(), [key])
    with pytest.raises(StorageUnavailable):
        await _collect(dead_store, _prefix())


def test_boto_loggers_warning_level() -> None:
    for name in ("boto3", "botocore", "s3transfer", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING, name
