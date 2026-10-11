"""The marketplace token envelope: the seal half (invariant 6; EXECUTION-PLAN decision 15).

`seal()` encrypts one access/refresh pair under a fresh 32-byte data key with AES-256-GCM, the
tenant id as AAD (a blob moved to another tenant's row never opens), and has the `KeyWrapper` wrap
that data key. The API and the worker may seal; only adapters and workers unseal
(`app.adapters.credentials`, kept there by the `decrypt-path-only-in-adapters-and-workers`
import contract).

Blob layout (all lengths in bytes): `MKT1` | u16 big-endian wrapped-key length | wrapped key |
12-byte nonce | ciphertext + 16-byte tag. The plaintext is compact JSON `{"v":1,"a":…,"r":…}`.

The wrapper is chosen at first use, never at import (CANON.md:163): `APP_ENV=local` gets
`LocalKmsStub` (AES-256-GCM under `KMS_LOCAL_DEV_KEY`, so a laptop and CI open each other's blobs);
anything else gets `CloudKmsKeyWrapper` (`app.adapters.kms`, S4) on `KMS_KEY_URI`.
Errors never carry key, token or blob bytes.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

if TYPE_CHECKING:
    from app.core.config import Settings

BLOB_MAGIC: Final = b"MKT1"
NONCE_BYTES: Final = 12
DATA_KEY_BYTES: Final = 32
TAG_BYTES: Final = 16
LOCAL_KEY_VERSION: Final = "local-1"
_HEADER: Final = struct.Struct(">H")
_MALFORMED: Final = "malformed token blob"


@dataclass(frozen=True, slots=True)
class TokenPair:
    """One OAuth token pair. `SecretStr` keeps both out of every repr, log line and message."""

    access_token: SecretStr
    refresh_token: SecretStr


@dataclass(frozen=True, slots=True)
class SealedTokens:
    blob: bytes = field(repr=False)
    key_version: str


@dataclass(frozen=True, slots=True)
class BlobParts:
    wrapped: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


class KeyWrapper(Protocol):
    async def wrap(self, dek: bytes, *, tenant_id: UUID) -> tuple[bytes, str]:
        """The wrapped data key and the key version that wrapped it."""
        ...


def wrap_aad(key_version: str, tenant_id: UUID) -> bytes:
    """The AAD binding a wrapped data key to its key version and tenant."""
    return b"mkos-dek|" + key_version.encode("ascii") + b"|" + tenant_id.bytes


def local_key(key_b64: str) -> bytes:
    """`KMS_LOCAL_DEV_KEY` as 32 raw bytes; anything else is a `ValueError` with no value."""
    try:
        key = base64.b64decode(key_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError("KMS_LOCAL_DEV_KEY must be base64 of 32 bytes") from None
    if len(key) != DATA_KEY_BYTES:
        raise ValueError("KMS_LOCAL_DEV_KEY must be base64 of 32 bytes")
    return key


class LocalKmsStub:
    """The local key-encryption key (A§9): wrap only. Never used outside `APP_ENV=local`."""

    def __init__(self, key_b64: str) -> None:
        self._aead = AESGCM(local_key(key_b64))

    def __repr__(self) -> str:
        return "LocalKmsStub()"

    async def wrap(self, dek: bytes, *, tenant_id: UUID) -> tuple[bytes, str]:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, dek, wrap_aad(LOCAL_KEY_VERSION, tenant_id))
        return nonce + sealed, LOCAL_KEY_VERSION


def _settings(settings: Settings | None) -> Settings:
    if settings is not None:
        return settings
    from app.core.config import get_settings

    return get_settings()


def kms_key_uri(settings: Settings) -> str:
    """The deployed token key; unset is a `RuntimeError` (the api and worker boot refuse it)."""
    if not settings.kms_key_uri:
        raise RuntimeError(f"APP_ENV={settings.app_env} needs KMS_KEY_URI to seal or unseal")
    return settings.kms_key_uri


def get_key_wrapper(settings: Settings | None = None) -> KeyWrapper:
    """The wrapper for this environment, built on each call (cheap), never at import."""
    resolved = _settings(settings)
    if resolved.app_env == "local":
        return LocalKmsStub(resolved.kms_local_dev_key)
    from app.adapters.kms import CloudKmsKeyWrapper

    return CloudKmsKeyWrapper(kms_key_uri(resolved))


def _plaintext(tokens: TokenPair) -> bytes:
    body = {
        "v": 1,
        "a": tokens.access_token.get_secret_value(),
        "r": tokens.refresh_token.get_secret_value(),
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def seal(tokens: TokenPair, *, tenant_id: UUID, wrapper: KeyWrapper) -> SealedTokens:
    """Encrypt `tokens` for `tenant_id` under a fresh data key and nonce; wrap the data key."""
    if not isinstance(tenant_id, UUID):
        raise TypeError("tenant_id must be a UUID")
    dek = os.urandom(DATA_KEY_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, _plaintext(tokens), tenant_id.bytes)
    wrapped, key_version = await wrapper.wrap(dek, tenant_id=tenant_id)
    if not wrapped or len(wrapped) > 0xFFFF:
        raise ValueError("wrapped key length out of range")
    blob = BLOB_MAGIC + _HEADER.pack(len(wrapped)) + wrapped + nonce + ciphertext
    return SealedTokens(blob=blob, key_version=key_version)


def parse_blob(blob: bytes) -> BlobParts:
    """Split a blob into its parts without decrypting. A bad magic, a length that runs past the
    end, or a tail too short for a nonce and a tag is `ValueError("malformed token blob")`."""
    head = len(BLOB_MAGIC) + _HEADER.size
    if not isinstance(blob, bytes | bytearray | memoryview):
        raise ValueError(_MALFORMED)
    data = bytes(blob)
    if len(data) < head or data[: len(BLOB_MAGIC)] != BLOB_MAGIC:
        raise ValueError(_MALFORMED)
    (wrapped_len,) = _HEADER.unpack_from(data, len(BLOB_MAGIC))
    wrapped_end = head + wrapped_len
    if wrapped_len == 0 or wrapped_end + NONCE_BYTES + TAG_BYTES + 1 > len(data):
        raise ValueError(_MALFORMED)
    return BlobParts(
        wrapped=data[head:wrapped_end],
        nonce=data[wrapped_end : wrapped_end + NONCE_BYTES],
        ciphertext=data[wrapped_end + NONCE_BYTES :],
    )
