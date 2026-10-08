"""Where brand documents are stored (S16 T11; PLAN D7): one narrow store interface.

`DocumentStore` is the S13b `ObjectStorage` shape (`put(bucket, key, data, content_type)`,
`get(bucket, key) -> bytes`, `delete(bucket, key)`; a missing object raises `StorageNotFound`,
an unreachable store `StorageUnavailable`). `get_document_store()` is the one provider: the
S3-API `get_storage()` (MinIO locally, GCS in the cloud) unless a test installed a store with
`use_document_store`.

Objects live in `GCS_BUCKET_CSV` under the tenant prefix, `<tenant_id>/brand-documents/<id>`.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.adapters.storage import StorageNotFound, StorageUnavailable, get_storage
from app.core.config import get_settings

__all__ = [
    "DocumentStore",
    "StorageNotFound",
    "StorageUnavailable",
    "brand_bucket",
    "brand_document_object_key",
    "get_document_store",
    "use_document_store",
]


class DocumentStore(Protocol):
    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, bucket: str, key: str) -> bytes: ...

    async def delete(self, bucket: str, key: str) -> None: ...


_store: DocumentStore | None = None


def use_document_store(store: DocumentStore | None) -> None:
    global _store
    _store = store


def get_document_store() -> DocumentStore:
    return get_storage() if _store is None else _store


def brand_document_object_key(tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    if not isinstance(tenant_id, uuid.UUID) or not isinstance(document_id, uuid.UUID):
        raise ValueError("tenant_id and document_id must be UUIDs")
    return f"{tenant_id}/brand-documents/{document_id}"


def brand_bucket() -> str:
    return get_settings().gcs_bucket_csv
