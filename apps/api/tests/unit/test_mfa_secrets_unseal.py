"""DPP-1 AC4: the MFA unseal adapter against a fake Cloud KMS client, and the label split between
MFA secrets and marketplace token blobs."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app.adapters.kms import CloudKmsKeyWrapper
from app.adapters.mfa_secrets import MfaUnsealError, _CloudUnwrap, _LocalUnwrap, unseal_bytes
from app.core.crypto import (
    MFA_BLOB_MAGIC,
    MFA_DEK_LABEL,
    LocalKmsStub,
    mfa_aad,
    seal_bytes,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from tests.unit.test_crypto_bytes import CI_KEY

MFA_KEY = (
    "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/mfa-secret-key"
)
TOKEN_KEY = "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key"
USER = uuid.UUID("11111111-2222-4333-8444-555555555555")


class FakeKms:
    """Cloud KMS stand-in: AES-GCM under one key, the AAD checked like KMS does."""

    def __init__(self) -> None:
        self._aead = AESGCM(AESGCM.generate_key(256))
        self.decrypts: list[dict[str, Any]] = []
        self.fail: Exception | None = None

    async def encrypt(self, *, request: dict[str, Any], **_: Any) -> SimpleNamespace:
        nonce = os.urandom(12)
        sealed = self._aead.encrypt(
            nonce, request["plaintext"], request["additional_authenticated_data"]
        )
        return SimpleNamespace(
            ciphertext=nonce + sealed, name=f"{request['name']}/cryptoKeyVersions/1"
        )

    async def decrypt(self, *, request: dict[str, Any], **_: Any) -> SimpleNamespace:
        self.decrypts.append(request)
        if self.fail is not None:
            raise self.fail
        data = request["ciphertext"]
        plain = self._aead.decrypt(data[:12], data[12:], request["additional_authenticated_data"])
        return SimpleNamespace(plaintext=plain)


def _cloud_seal(kms: FakeKms, *, label: bytes = MFA_DEK_LABEL) -> tuple[bytes, str]:
    wrapper = CloudKmsKeyWrapper(MFA_KEY, client=kms, label=label)
    sealed = asyncio.run(
        seal_bytes(
            b"\x07" * 20, aad=mfa_aad(USER), subject=USER, wrapper=wrapper, magic=MFA_BLOB_MAGIC
        )
    )
    return sealed.blob, sealed.key_version


def _cloud_open(kms: FakeKms, blob: bytes, version: str, key: str = MFA_KEY) -> bytes:
    return asyncio.run(
        unseal_bytes(blob, version, user_id=USER, unwrapper=_CloudUnwrap(key, client=kms))
    )


def test_cloud_round_trip() -> None:
    kms = FakeKms()
    blob, version = _cloud_seal(kms)
    assert version == f"{MFA_KEY}/cryptoKeyVersions/1"
    assert _cloud_open(kms, blob, version) == b"\x07" * 20
    assert kms.decrypts[0]["name"] == MFA_KEY


def test_wrong_key_version_prefix_fails_before_the_call() -> None:
    kms = FakeKms()
    blob, _ = _cloud_seal(kms)
    with pytest.raises(MfaUnsealError):
        _cloud_open(kms, blob, f"{TOKEN_KEY}/cryptoKeyVersions/1")
    with pytest.raises(MfaUnsealError):
        _cloud_open(kms, blob, f"{TOKEN_KEY}/cryptoKeyVersions/1", key=MFA_KEY)
    assert kms.decrypts == []


def test_kms_exception_becomes_the_fixed_error() -> None:
    kms = FakeKms()
    blob, version = _cloud_seal(kms)
    kms.fail = RuntimeError("PermissionDenied: secret detail projects/x")
    with pytest.raises(MfaUnsealError) as info:
        _cloud_open(kms, blob, version)
    assert str(info.value) == "unseal failed"
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_cloud_token_label_does_not_open_as_mfa() -> None:
    kms = FakeKms()
    blob, version = _cloud_seal(kms, label=b"mkos-dek|")
    with pytest.raises(MfaUnsealError):
        _cloud_open(kms, blob, version)


def test_mfa_label_differs_from_token_label() -> None:
    token_wrapped, version = asyncio.run(LocalKmsStub(CI_KEY).wrap(b"k" * 32, tenant_id=USER))
    with pytest.raises(Exception):  # noqa: B017 - InvalidTag from AES-GCM
        asyncio.run(_LocalUnwrap(CI_KEY).unwrap(token_wrapped, key_version=version, user_id=USER))
    mfa_wrapped, version = asyncio.run(
        LocalKmsStub(CI_KEY, label=MFA_DEK_LABEL).wrap(b"k" * 32, tenant_id=USER)
    )
    dek = asyncio.run(_LocalUnwrap(CI_KEY).unwrap(mfa_wrapped, key_version=version, user_id=USER))
    assert dek == b"k" * 32
    from app.core.crypto import DEK_LABEL

    assert DEK_LABEL not in (MFA_DEK_LABEL,)


def test_reprs_hold_no_key_material() -> None:
    assert repr(_LocalUnwrap(CI_KEY)) == "_LocalUnwrap()"
    assert CI_KEY not in repr(_CloudUnwrap(MFA_KEY))
