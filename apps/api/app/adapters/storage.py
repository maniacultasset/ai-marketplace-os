"""Object storage through the S3 API (S13 PD8; ADR-0001: MinIO locally, GCS's S3 interface in the
cloud). The one module that imports boto3/botocore (`.importlinter`).

`ObjectStorage.put(bucket, key, data, content_type)` and `.get(bucket, key) -> bytes` run the
blocking client in a thread (`asyncio.to_thread`); a missing object is `StorageNotFound`. These
names and the two positional arguments of `get` are the interface later steps extend: they add
methods and defaulted keyword-only parameters only, never rename or make one required.

`import_object_key(tenant_id, import_id)` is the CSV object key, always under the tenant prefix
(`<tenant_id>/imports/<import_id>.csv`). `get_storage()` builds the client from the S3 settings
with explicit connect and read timeouts, so a slow store fails fast instead of hanging a caller.
Nothing here logs or reprs a credential.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache
from typing import Any, Final

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from app.core.config import get_settings

CONNECT_TIMEOUT_S: Final = 5
READ_TIMEOUT_S: Final = 30
MAX_ATTEMPTS: Final = 2
_MISSING: Final = frozenset({"NoSuchKey", "404", "NotFound"})


class StorageNotFound(Exception):  # noqa: N818 - the name later steps import
    """The object does not exist. The message names neither the bucket nor the key."""


def import_object_key(tenant_id: uuid.UUID, import_id: uuid.UUID) -> str:
    if not isinstance(tenant_id, uuid.UUID) or not isinstance(import_id, uuid.UUID):
        raise ValueError("tenant_id and import_id must be UUIDs")
    return f"{tenant_id}/imports/{import_id}.csv"


class ObjectStorage:
    """A thin async face over one boto3 S3 client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __repr__(self) -> str:
        return "ObjectStorage(<s3 client>)"

    async def put(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, bucket: str, key: str) -> bytes:
        def read() -> bytes:
            try:
                response = self._client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code in _MISSING:
                    raise StorageNotFound("the object does not exist") from None
                raise
            body = response["Body"]
            try:
                data: bytes = body.read()
            finally:
                body.close()
            return data

        return await asyncio.to_thread(read)


def new_storage() -> ObjectStorage:
    settings = get_settings()
    client = boto3.session.Session().client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name="us-east-1",
        config=Config(
            connect_timeout=CONNECT_TIMEOUT_S,
            read_timeout=READ_TIMEOUT_S,
            retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"},
            s3={"addressing_style": "path"},
            signature_version="s3v4",
        ),
    )
    return ObjectStorage(client)


@lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    return new_storage()


def csv_bucket() -> str:
    return get_settings().gcs_bucket_csv
