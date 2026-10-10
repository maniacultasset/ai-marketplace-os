"""Object storage through the S3 API (S13 PD8; ADR-0001: MinIO locally, GCS's S3 interface in the
cloud). The one module that imports boto3/botocore (`.importlinter`).

`ObjectStorage.put(bucket, key, data, content_type)` and `.get(bucket, key) -> bytes` run the
blocking client in a thread (`asyncio.to_thread`); a missing object is `StorageNotFound`, and a transport failure (a timeout, a refused
connection, a 5xx) on `get` is `StorageUnavailable`. These
names and the two positional arguments of `get` are the interface later steps extend: they add
methods and defaulted keyword-only parameters only, never rename or make one required.

`import_object_key(tenant_id, import_id)` is the CSV object key, always under the tenant prefix
(`<tenant_id>/imports/<import_id>.csv`). `get_storage()` builds the client from the S3 settings
with explicit connect and read timeouts, so a slow store fails fast instead of hanging a caller.
Nothing here logs or reprs a credential.

S17b adds the image pipeline's calls: `presign_put` (a create-only signed PUT: the URL can create
the object but never overwrite it, because `If-None-Match: *` is signed), `head`, `get(...,
max_bytes=)`, `delete`, `delete_many` and the paged `iter_older_than` (the orphan sweep). S21
adds `presign_get` (a V4 signed GET the marketplace adapter hands over as an image URL; computed
locally, never logged, never stored).
`get_storage()` refuses the committed `local_only_` MinIO values, blank keys and a non-https
endpoint outside `APP_ENV=local` (`StorageConfigError`, KNOWN-ISSUES D), whatever the role. The
boto loggers are pinned to WARNING, so a DEBUG root logger never prints a signed URL.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Final

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)

from app.core.config import Settings, get_settings

CONNECT_TIMEOUT_S: Final = 5
READ_TIMEOUT_S: Final = 30
MAX_ATTEMPTS: Final = 2
_MISSING: Final = frozenset({"NoSuchKey", "404", "NotFound"})
_UNAVAILABLE: Final = frozenset(
    {"SlowDown", "ServiceUnavailable", "InternalError", "RequestTimeout"}
)
DELETE_BATCH_MAX: Final = 1000
LOCAL_ONLY_PREFIX: Final = "local_only_"
# The boto loggers print request URLs (a signed one included) at DEBUG; never below WARNING.
BOTO_LOGGERS: Final = ("boto3", "botocore", "s3transfer", "urllib3")
for _name in BOTO_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)


class StorageNotFound(Exception):  # noqa: N818 - the name later steps import
    """The object does not exist. The message names neither the bucket nor the key."""


class StorageUnavailable(Exception):  # noqa: N818 - a transport failure, retried by callers
    """The store could not be reached or answered 5xx (a timeout, a refused connection)."""


class StorageObjectTooLarge(Exception):  # noqa: N818 - a domain stop, never retried
    """`get(..., max_bytes=n)` found more than `n` bytes; at most `n + 1` were read."""


class StorageConfigError(RuntimeError):
    """The S3 settings are not deployable outside `APP_ENV=local`. Messages name variables only."""


@dataclass(frozen=True, slots=True)
class ObjectHead:
    size: int
    content_type: str | None
    last_modified: datetime


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    last_modified: datetime


def storage_config_errors(settings: Settings) -> list[str]:
    """Outside `APP_ENV=local`: both keys set, neither a `local_only_` value (any case, padded),
    and an https endpoint. Pure; the messages never carry a value."""
    if settings.app_env == "local":
        return []
    errors: list[str] = []
    keys = (settings.s3_access_key or "", settings.s3_secret_key or "")
    if any(not k.strip() for k in keys):
        errors.append("S3_ACCESS_KEY and S3_SECRET_KEY must be set outside APP_ENV=local")
    elif any(k.strip().casefold().startswith(LOCAL_ONLY_PREFIX) for k in keys):
        errors.append("S3 credentials are local-only values")
    if not (settings.s3_endpoint_url or "").strip().casefold().startswith("https://"):
        errors.append("S3_ENDPOINT_URL must use https outside APP_ENV=local")
    return errors


def _client_error(exc: ClientError) -> tuple[str, int]:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code, status


def _unavailable(code: str, status: int) -> bool:
    return status >= 500 or code in _UNAVAILABLE


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
        """Write the object. A 5xx or an unreachable store is `StorageUnavailable` (retryable),
        as on `get` (KNOWN-ISSUES §E, S13b: `put` used to map nothing)."""

        def write() -> None:
            try:
                self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
            except ClientError as exc:
                code, status = _client_error(exc)
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None

        await asyncio.to_thread(write)

    async def get(self, bucket: str, key: str, *, max_bytes: int | None = None) -> bytes:
        """The object's bytes. `max_bytes=None` reads it whole (the S13 call); a given cap reads
        at most `max_bytes + 1` bytes and raises `StorageObjectTooLarge` past it."""
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be >= 0")

        def read() -> bytes:
            try:
                response = self._client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code, status = _client_error(exc)
                if code in _MISSING:
                    raise StorageNotFound("the object does not exist") from None
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None
            body = response["Body"]
            try:
                data: bytes = body.read() if max_bytes is None else body.read(max_bytes + 1)
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None
            finally:
                body.close()
            if max_bytes is not None and len(data) > max_bytes:
                raise StorageObjectTooLarge("the object is over the read cap")
            return data

        return await asyncio.to_thread(read)

    async def presign_put(
        self, bucket: str, key: str, *, content_type: str, content_length: int, expires_s: int
    ) -> str:
        """A V4 signed PUT for exactly this key, type and length, valid `expires_s` seconds. The
        signed `If-None-Match: *` makes it create-only: a second PUT of the URL gets 412 and the
        first bytes stay. No network call."""
        if content_length < 1 or expires_s < 1:
            raise ValueError("content_length and expires_s must be positive")
        url: str = self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": content_type,
                "ContentLength": content_length,
                "IfNoneMatch": "*",
            },
            ExpiresIn=expires_s,
            HttpMethod="PUT",
        )
        return url

    async def presign_get(self, bucket: str, key: str, *, expires_s: int) -> str:
        """A V4 signed GET of exactly this key, valid `expires_s` seconds (S21 T2a: the image URL a
        publish hands the adapter). No network call. The URL is a bearer credential for the
        object: callers keep it in memory and never log, store or report it."""
        if expires_s < 1:
            raise ValueError("expires_s must be positive")
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_s,
            HttpMethod="GET",
        )
        return url

    async def head(self, bucket: str, key: str) -> ObjectHead | None:
        """The object's size, type and last-modified time; None when it does not exist."""

        def call() -> ObjectHead | None:
            try:
                meta = self._client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code, status = _client_error(exc)
                if code in _MISSING or status == 404:
                    return None
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None
            return ObjectHead(
                size=int(meta["ContentLength"]),
                content_type=meta.get("ContentType"),
                last_modified=meta["LastModified"],
            )

        return await asyncio.to_thread(call)

    async def delete(self, bucket: str, key: str) -> None:
        """Delete one object; a missing one is not an error."""

        def call() -> None:
            try:
                self._client.delete_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                code, status = _client_error(exc)
                if code in _MISSING:
                    return
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None

        await asyncio.to_thread(call)

    async def delete_many(self, bucket: str, keys: Sequence[str]) -> None:
        """One `delete_objects` call for 1..1000 keys (quiet). A per-key failure raises
        `StorageUnavailable` naming the count only."""
        if not 1 <= len(keys) <= DELETE_BATCH_MAX:
            raise ValueError(f"delete_many takes 1..{DELETE_BATCH_MAX} keys")

        def call() -> None:
            try:
                response = self._client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
                )
            except ClientError as exc:
                code, status = _client_error(exc)
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None
            failed = response.get("Errors") or []
            if failed:
                raise StorageUnavailable(f"{len(failed)} object(s) were not deleted")

        await asyncio.to_thread(call)

    async def iter_older_than(
        self,
        bucket: str,
        prefix: str,
        older_than: datetime,
        *,
        start_after: str | None = None,
        page_size: int = DELETE_BATCH_MAX,
    ) -> AsyncIterator[list[ObjectInfo]]:
        """Every object under `prefix` (which must end with `/`), one `list_objects_v2` page per
        item, each filtered to `last_modified < older_than`; `start_after` (under `prefix`)
        starts the listing after that key."""
        if not prefix.endswith("/"):
            raise ValueError("prefix must end with '/'")
        if start_after is not None and not start_after.startswith(prefix):
            raise ValueError("start_after must be under prefix")
        if not 1 <= page_size <= DELETE_BATCH_MAX:
            raise ValueError(f"page_size must be 1..{DELETE_BATCH_MAX}")

        def page(token: str | None) -> dict[str, Any]:
            params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": page_size}
            if token is not None:
                params["ContinuationToken"] = token
            elif start_after is not None:
                params["StartAfter"] = start_after
            try:
                result: dict[str, Any] = self._client.list_objects_v2(**params)
            except ClientError as exc:
                code, status = _client_error(exc)
                if _unavailable(code, status):
                    raise StorageUnavailable("the store answered 5xx") from None
                raise
            except BotoCoreError:
                raise StorageUnavailable("the store could not be reached") from None
            return result

        token: str | None = None
        while True:
            result = await asyncio.to_thread(page, token)
            yield [
                ObjectInfo(key=o["Key"], size=int(o["Size"]), last_modified=o["LastModified"])
                for o in result.get("Contents", [])
                if o["LastModified"] < older_than
            ]
            token = result.get("NextContinuationToken") if result.get("IsTruncated") else None
            if token is None:
                return


def new_storage() -> ObjectStorage:
    """The client from the S3 settings; `StorageConfigError` first when they are not deployable
    outside `APP_ENV=local`. The credentials are always passed explicitly, never the default
    AWS chain."""
    settings = get_settings()
    errors = storage_config_errors(settings)
    if errors:
        raise StorageConfigError("; ".join(errors))
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


def images_bucket() -> str:
    return get_settings().gcs_bucket_images


def submissions_bucket() -> str:
    return get_settings().gcs_bucket_submissions
