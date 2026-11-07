"""SI1 T19 (CP7, ADR-0026): the public image bucket on the compose MinIO for tests. The fixture
creates `marketplace-images-public`, sets the same GetObject-only bucket policy the compose
`minio-init-public` one-shot applies (the policy file is shared, so they cannot drift) and turns
on public hosting in the settings."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

PUBLIC_BUCKET = "marketplace-images-public"
POLICY_FILE = Path(__file__).resolve().parents[4] / "infra" / "minio" / "public-read.json"


@pytest.fixture
def public_hosting(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The public base URL (`<S3_ENDPOINT_URL>/marketplace-images-public`)."""
    from app.adapters.storage import get_storage
    from app.core.config import get_settings

    endpoint = get_settings().s3_endpoint_url.rstrip("/")
    base = f"{endpoint}/{PUBLIC_BUCKET}"
    monkeypatch.setenv("GCS_BUCKET_IMAGES_PUBLIC", PUBLIC_BUCKET)
    monkeypatch.setenv("IMAGE_PUBLIC_BASE_URL", base)
    get_settings.cache_clear()
    get_storage.cache_clear()
    client: Any = get_storage()._client
    try:
        client.create_bucket(Bucket=PUBLIC_BUCKET)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except client.exceptions.BucketAlreadyExists:
        pass
    client.put_bucket_policy(Bucket=PUBLIC_BUCKET, Policy=POLICY_FILE.read_text(encoding="utf-8"))
    yield base
    get_settings.cache_clear()
    get_storage.cache_clear()


async def put_clean(
    tenant_id: uuid.UUID, product_id: uuid.UUID, image_id: uuid.UUID, data: bytes, conn: Any
) -> str:
    """Store `data` as the image's clean copy and record its hash and type on the row."""
    from app.adapters.storage import get_storage, images_bucket
    from app.services.image_keys import derived_key

    await get_storage().put(
        images_bucket(),
        derived_key(tenant_id, product_id, image_id, "clean.jpg"),
        data,
        "image/jpeg",
    )
    digest = hashlib.sha256(data).hexdigest()
    conn.execute(
        "UPDATE images SET clean_content_hash = %s, clean_mime_type = 'image/jpeg', "
        "metadata = metadata || %s::jsonb WHERE id = %s",
        [digest, json.dumps({"vision": {"w": 10, "h": 10}}), image_id],
    )
    return digest
