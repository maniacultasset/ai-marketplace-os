"""The Cloud KMS key wrapper (ADR-0018; S4): the deployed key-encryption key of the token envelope.

Staging and production wrap each data key with `encrypt` on the `KMS_KEY_URI` CryptoKey; the
unwrap side is `CloudKmsKeyUnwrapper` in `app.adapters.credentials` (the decrypt path). The AAD
is `wrap_aad(KMS_KEY_URI, tenant)`: KMS picks the CryptoKeyVersion only while it encrypts, so the
key name fills the version slot, and the version KMS used (the reply's `name`) is what
`kms_key_version` stores, for audit and the rotation re-wrap. `decrypt` finds the version itself.

The client is `KeyManagementServiceAsyncClient` on Application Default Credentials (the Cloud Run
service account): built at first use, once per process and event loop (a gRPC aio channel is
bound to its loop), never at import. This is the one module that imports `google.cloud.kms`
(import contract `kms-client-only-in-kms-adapter`). Every failure is a detail-free
`ValueError("key wrap failed")`, raised outside the `except` block, so no request, key or token
bytes ride on the error.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any, Final
from uuid import UUID

from google.cloud import kms

from app.core.crypto import DEK_LABEL, wrap_aad

KMS_TIMEOUT_SECONDS: Final = 10.0
_WRAP_FAILED: Final = "key wrap failed"
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = weakref.WeakKeyDictionary()


def kms_client() -> Any:
    """The process's KMS client for the running event loop, built on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = _clients[loop] = kms.KeyManagementServiceAsyncClient()
    return client


def version_prefix(key_uri: str) -> str:
    """Every CryptoKeyVersion of `key_uri` starts with this."""
    return f"{key_uri}/cryptoKeyVersions/"


class CloudKmsKeyWrapper:
    """Wrap only (the API's grant is encrypt). `client` is for tests; otherwise `kms_client()`."""

    def __init__(self, key_uri: str, *, client: Any = None, label: bytes = DEK_LABEL) -> None:
        self.key_uri = key_uri
        self._client = client
        self._label = label

    def __repr__(self) -> str:
        return f"CloudKmsKeyWrapper({self.key_uri!r})"

    async def wrap(self, dek: bytes, *, tenant_id: UUID) -> tuple[bytes, str]:
        request = {
            "name": self.key_uri,
            "plaintext": dek,
            "additional_authenticated_data": wrap_aad(self.key_uri, tenant_id, label=self._label),
        }
        wrapped, version = b"", ""
        try:
            client = self._client or kms_client()
            reply = await client.encrypt(request=request, timeout=KMS_TIMEOUT_SECONDS)
            wrapped, version = bytes(reply.ciphertext), str(reply.name)
        except Exception:
            wrapped = b""
        if not wrapped or not version.startswith(version_prefix(self.key_uri)):
            # raised outside the `except` block: no __context__ carries the KMS error
            raise ValueError(_WRAP_FAILED) from None
        return wrapped, version
