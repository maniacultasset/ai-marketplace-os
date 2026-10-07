"""S15 T4 (DW3, DW7, AC5): the unseal half, `app/adapters/credentials.py`. Only adapters and
workers import it (the `decrypt-path-only-in-adapters-and-workers` import contract). Every
failure is the one `CredentialUnsealError("unseal failed")`, with no detail and no cause chain."""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

if TYPE_CHECKING:
    from app.core.crypto import SealedTokens, TokenPair

TENANT = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER = uuid.UUID("99999999-2222-4333-8444-555555555555")


def _ci_key(repo_root: Path) -> str:
    """The CI key `.env.example` ships (A§9: a laptop and CI unseal each other's blobs)."""
    for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("KMS_LOCAL_DEV_KEY="):
            return line.split("=", 1)[1]
    raise AssertionError(".env.example has no KMS_LOCAL_DEV_KEY")


CI_KEY = _ci_key(Path(__file__).resolve().parents[4])


def _pair(access: str, refresh: str) -> TokenPair:
    from app.core.crypto import TokenPair

    return TokenPair(access_token=SecretStr(access), refresh_token=SecretStr(refresh))


def _seal(pair: TokenPair, tenant: uuid.UUID = TENANT, key: str = CI_KEY) -> SealedTokens:
    from app.core.crypto import LocalKmsStub, seal

    return asyncio.run(seal(pair, tenant_id=tenant, wrapper=LocalKmsStub(key)))


def _unseal(
    blob: bytes, *, key_version: str = "local-1", tenant: uuid.UUID = TENANT, key: str = CI_KEY
) -> TokenPair:
    from app.adapters.credentials import LocalKmsUnwrapStub, unseal

    return asyncio.run(
        unseal(blob, key_version=key_version, tenant_id=tenant, unwrapper=LocalKmsUnwrapStub(key))
    )


@pytest.mark.parametrize(
    ("access", "refresh"),
    [
        ("Atza|IwEBIA-access", "Atzr|IwEBIA-refresh"),
        ("ünïcödé-✓-访问", "refresh-😀-تحديث"),
        ("a" * 4096, "r" * 4096),
    ],
    ids=["ascii", "unicode", "4096"],
)
def test_keywrapper_roundtrip(access: str, refresh: str) -> None:
    sealed = _seal(_pair(access, refresh))
    assert access.encode() not in sealed.blob and refresh.encode() not in sealed.blob
    opened = _unseal(sealed.blob, key_version=sealed.key_version)
    assert opened.access_token.get_secret_value() == access
    assert opened.refresh_token.get_secret_value() == refresh


def _assert_unseal_fails(blob: bytes, **kwargs: object) -> None:
    from app.adapters.credentials import CredentialUnsealError

    with pytest.raises(CredentialUnsealError) as info:
        _unseal(blob, **kwargs)  # type: ignore[arg-type]
    assert str(info.value) == "unseal failed"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


def test_unseal_other_tenant_fails() -> None:
    _assert_unseal_fails(_seal(_pair("a-token", "r-token")).blob, tenant=OTHER)


@pytest.mark.parametrize("part", ["header", "wrapped", "nonce", "ciphertext", "tag"])
def test_unseal_flipped_byte_fails(part: str) -> None:
    from app.core.crypto import parse_blob

    blob = bytearray(_seal(_pair("a-token", "r-token")).blob)
    parts = parse_blob(bytes(blob))
    wrapped_at = 6
    nonce_at = wrapped_at + len(parts.wrapped)
    ct_at = nonce_at + len(parts.nonce)
    index = {
        "header": 1,
        "wrapped": wrapped_at + 3,
        "nonce": nonce_at + 2,
        "ciphertext": ct_at + 1,
        "tag": len(blob) - 1,
    }[part]
    blob[index] ^= 0x01
    _assert_unseal_fails(bytes(blob))


def test_unseal_other_key_version_fails() -> None:
    _assert_unseal_fails(_seal(_pair("a-token", "r-token")).blob, key_version="local-2")


def test_unseal_other_local_key_fails() -> None:
    other_key = base64.b64encode(b"k" * 32).decode()
    _assert_unseal_fails(_seal(_pair("a-token", "r-token")).blob, key=other_key)


@pytest.mark.parametrize("blob", [b"", b"MKT1", b"\x00" * 200])
def test_unseal_garbage_fails(blob: bytes) -> None:
    _assert_unseal_fails(blob)


def test_unseal_error_has_no_detail() -> None:
    from app.adapters.credentials import CredentialUnsealError

    sealed = _seal(_pair("Zq9-access", "Zq9-refresh"))
    try:
        _unseal(sealed.blob, tenant=OTHER)
    except CredentialUnsealError as exc:
        for rendered in (str(exc), repr(exc)):
            assert "Zq9" not in rendered and "unseal failed" in rendered
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__
        assert exc.args == ("unseal failed",)
    else:
        raise AssertionError("unseal of another tenant's blob succeeded")


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_unwrapper_non_local_not_implemented(app_env: str) -> None:
    from app.adapters.credentials import get_key_unwrapper
    from app.core.config import Settings

    with pytest.raises(NotImplementedError, match="Cloud KMS"):
        get_key_unwrapper(Settings(_env_file=None, app_env=app_env, app_role="shell"))


def test_unwrapper_local_is_stub() -> None:
    from app.adapters.credentials import LocalKmsUnwrapStub, get_key_unwrapper
    from app.core.config import Settings

    unwrapper = get_key_unwrapper(Settings(_env_file=None, app_env="local", app_role="shell"))
    assert isinstance(unwrapper, LocalKmsUnwrapStub)


@settings(max_examples=60, deadline=None)
@given(
    access=st.text(min_size=1, max_size=600),
    refresh=st.text(min_size=1, max_size=600),
    tenant=st.uuids(),
)
def test_envelope_roundtrip_property(access: str, refresh: str, tenant: uuid.UUID) -> None:
    sealed = _seal(_pair(access, refresh), tenant=tenant)
    opened = _unseal(sealed.blob, key_version=sealed.key_version, tenant=tenant)
    assert opened.access_token.get_secret_value() == access
    assert opened.refresh_token.get_secret_value() == refresh
