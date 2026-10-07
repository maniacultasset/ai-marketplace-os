"""Where brand documents are stored (S16 T11; PLAN D7): one narrow store interface.

`DocumentStore` has the S13b `ObjectStorage` shapes (`put(bucket, key, data, content_type)`,
`get(bucket, key) -> bytes`, a missing object raises `StorageNotFound`) plus `delete(bucket, key)`
(CP10). `get_document_store()` is the one provider; until the S3-API adapter lands on this branch
(S13b's `app/adapters/storage.py`) no store is configured and it raises `StorageNotConfigured`,
so an upload fails closed instead of losing the file. Tests install a store with
`use_document_store`.

Objects live in `GCS_BUCKET_CSV` under the tenant prefix, `<tenant_id>/brand-documents/<id>`.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.core.config import get_settings


class StorageNotFound(Exception):  # noqa: N818 - the S13b name
    """The object does not exist. The message names neither the bucket nor the key."""


class StorageNotConfigured(Exception):  # noqa: N818 - a deployment fault, never a user error
    def __init__(self) -> None:
        super().__init__("no document store is configured")


class DocumentStore(Protocol):
    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, bucket: str, key: str) -> bytes: ...

    async def delete(self, bucket: str, key: str) -> None: ...


_store: DocumentStore | None = None


def use_document_store(store: DocumentStore | None) -> None:
    global _store
    _store = store


def get_document_store() -> DocumentStore:
    if _store is None:
        raise StorageNotConfigured
    return _store


def brand_document_object_key(tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
    if not isinstance(tenant_id, uuid.UUID) or not isinstance(document_id, uuid.UUID):
        raise ValueError("tenant_id and document_id must be UUIDs")
    return f"{tenant_id}/brand-documents/{document_id}"


def brand_bucket() -> str:
    return get_settings().gcs_bucket_csv
