"""DPP-1 AC4: `seal_bytes` (app.core.crypto) and `unseal_bytes` (app.adapters.mfa_secrets) for MFA
secrets: their own magic, AAD and data-key label, and one detail-free error on every failure."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from app.adapters.mfa_secrets import MfaUnsealError, _LocalUnwrap, unseal_bytes
from app.core.crypto import (
    MFA_BLOB_MAGIC,
    LocalKmsStub,
    SealedBytes,
    TokenPair,
    mfa_aad,
    seal,
    seal_bytes,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

USER = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER = uuid.UUID("99999999-2222-4333-8444-555555555555")
SECRET = b"\x01" * 20


def _ci_key() -> str:
    root = Path(__file__).resolve().parents[4]
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("KMS_LOCAL_DEV_KEY="):
            return line.split("=", 1)[1]
    raise AssertionError(".env.example has no KMS_LOCAL_DEV_KEY")


CI_KEY = _ci_key()


def _mfa_wrapper() -> LocalKmsStub:
    from app.core.crypto import MFA_DEK_LABEL

    return LocalKmsStub(CI_KEY, label=MFA_DEK_LABEL)


def _seal(secret: bytes = SECRET, user: uuid.UUID = USER, **over: object) -> SealedBytes:
    kwargs: dict[str, object] = {
        "aad": mfa_aad(user),
        "subject": user,
        "wrapper": _mfa_wrapper(),
        "magic": MFA_BLOB_MAGIC,
        **over,
    }
    return asyncio.run(seal_bytes(secret, **kwargs))  # type: ignore[arg-type]


def _unseal(blob: bytes, key_version: str = "local-1", user: uuid.UUID = USER) -> bytes:
    return asyncio.run(
        unseal_bytes(blob, key_version, user_id=user, unwrapper=_LocalUnwrap(CI_KEY))
    )


def _fails(blob: bytes, **kwargs: object) -> MfaUnsealError:
    with pytest.raises(MfaUnsealError) as info:
        _unseal(blob, **kwargs)  # type: ignore[arg-type]
    return info.value


def test_round_trip() -> None:
    sealed = _seal()
    assert sealed.blob.startswith(MFA_BLOB_MAGIC)
    assert sealed.key_version == "local-1"
    assert SECRET not in sealed.blob
    assert _unseal(sealed.blob) == SECRET
    assert "blob=" not in repr(sealed)


def test_other_user_aad_fails() -> None:
    _fails(_seal().blob, user=OTHER)


def test_token_aad_or_magic_fails() -> None:
    # an MKT1 token blob is refused by its magic
    pair = TokenPair(access_token=SecretStr("a"), refresh_token=SecretStr("r"))
    token_blob = asyncio.run(seal(pair, tenant_id=USER, wrapper=LocalKmsStub(CI_KEY))).blob
    _fails(token_blob)
    # an MFA-shaped blob whose data key was wrapped under the token label is refused too
    with_token_label = _seal(wrapper=LocalKmsStub(CI_KEY))
    _fails(with_token_label.blob)


def test_wrong_key_version_fails() -> None:
    _fails(_seal().blob, key_version="local-2")


REGIONS = {
    "header": 0,
    "length": 5,
    "wrapped": 10,
    "nonce": -40,
    "ciphertext": -20,
    "tag": -1,
}


@pytest.mark.parametrize("region", list(REGIONS))
def test_flipped_byte_each_region_fails(region: str) -> None:
    blob = bytearray(_seal().blob)
    blob[REGIONS[region]] ^= 0x01
    _fails(bytes(blob))


@pytest.mark.parametrize("cut", [0, 3, 6, 30, -1, -17])
def test_truncated_blob_fails(cut: int) -> None:
    blob = _seal().blob
    _fails(blob[:cut])


@pytest.mark.parametrize("bad", [b"", b"MFA1", None, "MFA1 not bytes"])
def test_garbage_fails(bad: object) -> None:
    _fails(bad)  # type: ignore[arg-type]


def test_error_message_has_no_bytes() -> None:
    blob = bytearray(_seal().blob)
    blob[-1] ^= 0x01
    err = _fails(bytes(blob))
    assert str(err) == "unseal failed"
    assert err.__cause__ is None
    assert repr(err.__context__) == "None"


def test_seal_bytes_refuses_bad_inputs() -> None:
    with pytest.raises(TypeError):
        _seal(subject="not-a-uuid")
    with pytest.raises(ValueError, match="magic"):
        _seal(magic=b"TOO-LONG")


def test_existing_token_blobs_still_open() -> None:
    from app.adapters.credentials import LocalKmsUnwrapStub, unseal

    pair = TokenPair(access_token=SecretStr("Atza|x"), refresh_token=SecretStr("Atzr|y"))
    sealed = asyncio.run(seal(pair, tenant_id=USER, wrapper=LocalKmsStub(CI_KEY)))
    opened = asyncio.run(
        unseal(
            sealed.blob,
            key_version=sealed.key_version,
            tenant_id=USER,
            unwrapper=LocalKmsUnwrapStub(CI_KEY),
        )
    )
    assert opened.access_token.get_secret_value() == "Atza|x"


def test_default_unwrapper_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    config.get_settings.cache_clear()
    sealed = _seal()
    assert asyncio.run(unseal_bytes(sealed.blob, sealed.key_version, user_id=USER)) == SECRET


@settings(max_examples=50, deadline=None)
@given(secret=st.binary(min_size=20, max_size=20), user=st.uuids())
def test_property_round_trip_any_20_bytes(secret: bytes, user: uuid.UUID) -> None:
    assert _unseal(_seal(secret, user).blob, user=user) == secret
