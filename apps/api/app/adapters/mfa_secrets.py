"""The MFA secret envelope: the unseal half (DPP-1, ADR-0021).

Only `app.services.mfa` imports this module (import contract
`mfa-secret-unseal-only-in-mfa-service`). It holds its own unwrap code for the MFA data-key label
rather than importing `app.adapters.credentials`, which would put the marketplace token decrypt
path behind the MFA service.

Every failure (a malformed blob, another user's blob, a token blob, a flipped byte, another key or
key version) is the one `MfaUnsealError("unseal failed")`, raised with `from None`, so no detail,
key or secret bytes reach a log line, a Sentry event or an ErrorReport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.crypto import (
    LOCAL_KEY_VERSION,
    MFA_BLOB_MAGIC,
    MFA_DEK_LABEL,
    NONCE_BYTES,
    local_key,
    mfa_aad,
    mfa_kms_key_uri,
    parse_blob,
    wrap_aad,
)

if TYPE_CHECKING:
    from app.core.config import Settings

_FAILED: Final = "unseal failed"


class MfaUnsealError(Exception):
    """A stored MFA secret could not be opened. Carries no detail by design."""

    def __init__(self) -> None:
        super().__init__(_FAILED)


class MfaKeyUnwrapper(Protocol):
    async def unwrap(self, wrapped: bytes, *, key_version: str, user_id: UUID) -> bytes: ...


class _LocalUnwrap:
    """The LocalKmsStub's unwrap side under the MFA label (`APP_ENV=local` only)."""

    def __init__(self, key_b64: str) -> None:
        self._aead = AESGCM(local_key(key_b64))

    def __repr__(self) -> str:
        return "_LocalUnwrap()"

    async def unwrap(self, wrapped: bytes, *, key_version: str, user_id: UUID) -> bytes:
        if key_version != LOCAL_KEY_VERSION or len(wrapped) <= NONCE_BYTES:
            raise ValueError(_FAILED)
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        aad = wrap_aad(key_version, user_id, label=MFA_DEK_LABEL)
        return self._aead.decrypt(nonce, sealed, aad)


class _CloudUnwrap:
    """Cloud KMS decrypt on `mfa-secret-key` (the API's grant there is encrypt and decrypt).

    A `key_version` that is not a CryptoKeyVersion of `key_uri` fails before any KMS call."""

    def __init__(self, key_uri: str, *, client: Any = None) -> None:
        self.key_uri = key_uri
        self._client = client

    def __repr__(self) -> str:
        return f"_CloudUnwrap({self.key_uri!r})"

    async def unwrap(self, wrapped: bytes, *, key_version: str, user_id: UUID) -> bytes:
        from app.adapters.kms import KMS_TIMEOUT_SECONDS, kms_client, version_prefix

        if not key_version.startswith(version_prefix(self.key_uri)) or not wrapped:
            raise ValueError(_FAILED)
        request = {
            "name": self.key_uri,
            "ciphertext": wrapped,
            "additional_authenticated_data": wrap_aad(self.key_uri, user_id, label=MFA_DEK_LABEL),
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


def get_mfa_key_unwrapper(settings: Settings | None = None) -> MfaKeyUnwrapper:
    """The unwrapper for this environment, built on each call, never at import."""
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    if settings.app_env == "local":
        return _LocalUnwrap(settings.kms_local_dev_key)
    return _CloudUnwrap(mfa_kms_key_uri(settings))


async def _open(blob: bytes, key_version: str, user_id: UUID, unwrapper: MfaKeyUnwrapper) -> bytes:
    parts = parse_blob(blob, magic=MFA_BLOB_MAGIC)
    dek = await unwrapper.unwrap(parts.wrapped, key_version=key_version, user_id=user_id)
    return AESGCM(dek).decrypt(parts.nonce, parts.ciphertext, mfa_aad(user_id))


async def unseal_bytes(
    blob: bytes,
    key_version: str,
    *,
    user_id: UUID,
    unwrapper: MfaKeyUnwrapper | None = None,
) -> bytes:
    """The MFA secret sealed for `user_id`; any failure is `MfaUnsealError`."""
    failed = False
    secret = b""
    try:
        if not isinstance(user_id, UUID) or not isinstance(key_version, str):
            raise ValueError(_FAILED)
        secret = await _open(blob, key_version, user_id, unwrapper or get_mfa_key_unwrapper())
    except Exception:
        failed = True
    if failed or not secret:
        # raised outside the `except` block: no __context__ carries the cause
        raise MfaUnsealError from None
    return secret
