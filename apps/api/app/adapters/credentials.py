"""The marketplace token envelope: the unseal half (invariant 6; EXECUTION-PLAN decision 15).

Only `app.adapters.*` and `app.workers.*` import this module, directly or indirectly: the
`decrypt-path-only-in-adapters-and-workers` import contract keeps it out of the API, the services,
the repositories and the core. Plaintext exists only in the caller's locals, as `SecretStr`, for
the one marketplace call that needs it (spec L12512, L12526).

Every failure (a malformed blob, another tenant's blob, a flipped byte, another key or key
version, a non-JSON body) is the one `CredentialUnsealError("unseal failed")`, raised with
`from None`, so no detail, key or token bytes reach a log line, a Sentry event or an ErrorReport.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from app.core.crypto import (
    LOCAL_KEY_VERSION,
    NONCE_BYTES,
    TokenPair,
    kms_key_uri,
    local_key,
    parse_blob,
    wrap_aad,
)

if TYPE_CHECKING:
    from app.core.config import Settings

_FAILED: Final = "unseal failed"


class CredentialUnsealError(Exception):
    """A stored credential could not be opened. Carries no detail by design."""

    def __init__(self) -> None:
        super().__init__(_FAILED)


class KeyUnwrapper(Protocol):
    async def unwrap(self, wrapped: bytes, *, key_version: str, tenant_id: UUID) -> bytes:
        """The data key `wrapped` holds, for `tenant_id` under `key_version`."""
        ...


class LocalKmsUnwrapStub:
    """The LocalKmsStub's unwrap side (`APP_ENV=local` only)."""

    def __init__(self, key_b64: str) -> None:
        self._aead = AESGCM(local_key(key_b64))

    def __repr__(self) -> str:
        return "LocalKmsUnwrapStub()"

    async def unwrap(self, wrapped: bytes, *, key_version: str, tenant_id: UUID) -> bytes:
        if key_version != LOCAL_KEY_VERSION or len(wrapped) <= NONCE_BYTES:
            raise ValueError(_FAILED)
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        return self._aead.decrypt(nonce, sealed, wrap_aad(key_version, tenant_id))


class CloudKmsKeyUnwrapper:
    """The Cloud KMS wrapper's unwrap side (S4; the worker's grant is encrypt and decrypt).

    A `key_version` that is not a CryptoKeyVersion of `key_uri` fails before any KMS call; KMS
    picks the version from the ciphertext, and the stored one stays for audit and rotation.
    `client` is for tests; otherwise the process's `app.adapters.kms.kms_client()`."""

    def __init__(self, key_uri: str, *, client: Any = None) -> None:
        self.key_uri = key_uri
        self._client = client

    def __repr__(self) -> str:
        return f"CloudKmsKeyUnwrapper({self.key_uri!r})"

    async def unwrap(self, wrapped: bytes, *, key_version: str, tenant_id: UUID) -> bytes:
        from app.adapters.kms import KMS_TIMEOUT_SECONDS, kms_client, version_prefix

        if not key_version.startswith(version_prefix(self.key_uri)) or not wrapped:
            raise ValueError(_FAILED)
        request = {
            "name": self.key_uri,
            "ciphertext": wrapped,
            "additional_authenticated_data": wrap_aad(self.key_uri, tenant_id),
        }
        dek = b""
        try:
            client = self._client or kms_client()
            reply = await client.decrypt(request=request, timeout=KMS_TIMEOUT_SECONDS)
            dek = bytes(reply.plaintext)
        except Exception:
            dek = b""
        if not dek:
            # raised outside the `except` block: no __context__ carries the KMS error
            raise ValueError(_FAILED) from None
        return dek


def get_key_unwrapper(settings: Settings | None = None) -> KeyUnwrapper:
    """The unwrapper for this environment, built on each call, never at import."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    if settings.app_env == "local":
        return LocalKmsUnwrapStub(settings.kms_local_dev_key)
    return CloudKmsKeyUnwrapper(kms_key_uri(settings))


def _pair(body: Any) -> TokenPair:
    if not isinstance(body, dict) or body.get("v") != 1:
        raise ValueError(_FAILED)
    access, refresh = body.get("a"), body.get("r")
    if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
        raise ValueError(_FAILED)
    return TokenPair(access_token=SecretStr(access), refresh_token=SecretStr(refresh))


async def _open(blob: bytes, key_version: str, tenant_id: UUID, unwrapper: KeyUnwrapper) -> Any:
    parts = parse_blob(blob)
    dek = await unwrapper.unwrap(parts.wrapped, key_version=key_version, tenant_id=tenant_id)
    plaintext = AESGCM(dek).decrypt(parts.nonce, parts.ciphertext, tenant_id.bytes)
    return _pair(json.loads(plaintext.decode("utf-8")))


async def unseal(
    blob: bytes, *, key_version: str, tenant_id: UUID, unwrapper: KeyUnwrapper
) -> TokenPair:
    """The token pair sealed for `tenant_id`; any failure is `CredentialUnsealError`."""
    failed = False
    try:
        if not isinstance(tenant_id, UUID) or not isinstance(key_version, str):
            raise ValueError(_FAILED)
        pair: TokenPair = await _open(blob, key_version, tenant_id, unwrapper)
    except Exception:
        failed = True
    if failed:
        # raised outside the `except` block: no __context__ to leak the cryptography error
        raise CredentialUnsealError from None
    return pair
