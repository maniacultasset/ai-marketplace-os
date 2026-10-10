"""S21 T8 (D5; CP12; AC2, AC14): snapshots and raw bodies in the submissions bucket (compose
MinIO): tenant-prefixed keys, digests over the stored bytes, no URL or token in a snapshot."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.adapters.base import AdapterListing
from app.adapters.storage import ObjectStorage, get_storage, new_storage, submissions_bucket
from app.services.submission_store import (
    BufferedSink,
    snapshot_bytes,
    write_raw,
    write_snapshot,
)
from mkos_schemas.listing import CanonicalListing

from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
CANARY = "CANARYzq9-0123456789abcdef"


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStorage]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_storage.cache_clear()
    client = new_storage()
    try:
        client._client.head_bucket(Bucket=submissions_bucket())
    except Exception:
        pytest.fail(f"MinIO bucket {submissions_bucket()} not reachable")
    yield client
    get_storage.cache_clear()
    get_settings.cache_clear()


def _item(image: uuid.UUID) -> AdapterListing:
    listing = CanonicalListing.model_validate(
        {
            "listing_id": str(uuid.uuid4()),
            "product_id": str(uuid.uuid4()),
            "marketplace": "amazon",
            "category": "ethnic_wear_kurta_sets",
            "version": 1,
            "content": {
                "title": "Cotton Kurta Set",
                "bullets": ["Soft cotton"],
                "description": "A cotton kurta set.",
            },
            "images": [{"image_id": str(image), "slot": "main"}],
        }
    )
    assert listing.listing_id is not None
    return AdapterListing(
        listing_id=listing.listing_id,
        seller_sku="KURTA-1",
        idempotency_key="k",
        listing=listing,
        image_refs={image: f"https://minio.local/x?X-Amz-Signature={CANARY}"},
    )


async def test_snapshot_has_no_url_or_token(storage: ObjectStorage) -> None:
    image = uuid.uuid4()
    data = snapshot_bytes(
        _item(image), operation="create", field_paths=None, image_tags={image: f"image:{image}@ab"}
    )
    text = data.decode()
    assert CANARY not in text and "https://" not in text and "X-Amz" not in text
    assert f"image:{image}@ab" in text


async def test_snapshot_sha_matches_bytes_and_key_under_tenant(storage: ObjectStorage) -> None:
    tenant, submission = uuid.uuid4(), uuid.uuid4()
    image = uuid.uuid4()
    data = snapshot_bytes(_item(image), operation="create", field_paths=None, image_tags={})
    key, sha = await write_snapshot(storage, tenant, submission, data)
    try:
        assert key == f"{tenant}/submissions/{submission}/payload.json"
        stored = await storage.get(submissions_bucket(), key)
        assert hashlib.sha256(stored).digest() == sha and stored == data
    finally:
        await storage.delete(submissions_bucket(), key)


async def test_raw_sha_matches_stored_and_buffer_order(storage: ObjectStorage) -> None:
    tenant, submission = uuid.uuid4(), uuid.uuid4()
    sink = BufferedSink()
    sink.put("SKU-1", "put", b'{"status":"ACCEPTED"}')
    sink.put("SKU-2", "put", b'{"status":"INVALID"}')
    stored = await write_raw(storage, tenant, submission, sink)
    assert stored is not None
    key, sha = stored
    try:
        data = await storage.get(submissions_bucket(), key)
        assert hashlib.sha256(data).digest() == sha
        lines = data.decode().splitlines()
        assert ['"SKU-1"' in lines[0], '"SKU-2"' in lines[1]] == [True, True]
    finally:
        await storage.delete(submissions_bucket(), key)
    assert await write_raw(storage, tenant, submission, BufferedSink()) is None
