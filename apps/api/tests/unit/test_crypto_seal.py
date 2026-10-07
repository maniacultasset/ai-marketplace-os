"""S15 T3 (DW3, AC5): the token envelope's seal half, `app/core/crypto.py`. A fresh 32-byte data
key and 96-bit nonce per seal, AES-256-GCM with the tenant as AAD, the data key wrapped by the
`KeyWrapper` (LocalKmsStub when `APP_ENV=local`). No decryption here: unseal is adapter-only."""

from __future__ import annotations

import asyncio
import base64
import importlib
import struct
import sys
import uuid
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

if TYPE_CHECKING:
    from app.core.crypto import SealedTokens, TokenPair

CI_KEY = "bG9jYWxfZGV2X29ubHlfMzJfYnl0ZV9rZXlfMDAwMDA="  # .env.example KMS_LOCAL_DEV_KEY
TENANT = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _pair(
    access: str = "Atza|access-token-value", refresh: str = "Atzr|refresh-token-value"
) -> TokenPair:
    from app.core.crypto import TokenPair

    return TokenPair(access_token=SecretStr(access), refresh_token=SecretStr(refresh))


def _seal(pair: TokenPair, tenant: uuid.UUID = TENANT) -> SealedTokens:
    from app.core.crypto import LocalKmsStub, seal

    return asyncio.run(seal(pair, tenant_id=tenant, wrapper=LocalKmsStub(CI_KEY)))


def test_seal_blob_layout() -> None:
    from app.core.crypto import BLOB_MAGIC, NONCE_BYTES, parse_blob

    sealed = _seal(_pair())
    assert sealed.key_version == "local-1"
    blob = sealed.blob
    assert blob[:4] == BLOB_MAGIC == b"MKT1"
    (wrapped_len,) = struct.unpack(">H", blob[4:6])
    parts = parse_blob(blob)
    assert len(parts.wrapped) == wrapped_len
    # the stub's wrapped key: its own nonce, the 32-byte data key and the GCM tag
    assert wrapped_len == 12 + 32 + 16
    assert len(parts.nonce) == NONCE_BYTES == 12
    assert blob == b"MKT1" + blob[4:6] + parts.wrapped + parts.nonce + parts.ciphertext
    assert len(parts.ciphertext) > 16


def test_two_seals_differ() -> None:
    from app.core.crypto import parse_blob

    pair = _pair()
    one, two = parse_blob(_seal(pair).blob), parse_blob(_seal(pair).blob)
    assert one.nonce != two.nonce
    assert one.wrapped != two.wrapped
    assert one.ciphertext != two.ciphertext


def _windows(value: str) -> set[bytes]:
    raw = value.encode("utf-8")
    if len(raw) < 8:
        return {raw}
    return {raw[i : i + 8] for i in range(len(raw) - 7)}


@settings(max_examples=60, deadline=None)
@given(
    access=st.text(min_size=1, max_size=4096),
    refresh=st.text(min_size=1, max_size=4096),
)
def test_seal_no_plaintext_run_8_bytes(access: str, refresh: str) -> None:
    blob = _seal(_pair(access, refresh)).blob
    for token in (access, refresh):
        windows = _windows(token)
        if len(token.encode("utf-8")) < 8:
            continue  # a 1-7 byte run can occur in random bytes by chance; 8+ cannot (2^-64)
        assert not any(w in blob for w in windows)


@pytest.mark.parametrize("case", ["magic", "overrun", "truncated", "empty"])
def test_parse_blob_rejects(case: str) -> None:
    from app.core.crypto import parse_blob

    blob = _seal(_pair()).blob
    bad = {
        "magic": b"MKT2" + blob[4:],
        "overrun": blob[:4] + struct.pack(">H", 65535) + blob[6:],
        "truncated": blob[: 6 + 60 + 12 + 10],
        "empty": b"",
    }[case]
    with pytest.raises(ValueError) as info:
        parse_blob(bad)
    assert str(info.value) == "malformed token blob"


@pytest.mark.parametrize(
    "key",
    [
        base64.b64encode(b"x" * 31).decode(),
        base64.b64encode(b"x" * 33).decode(),
        "not base64 at all!",
        "",
    ],
)
def test_local_stub_rejects_bad_key_length(key: str) -> None:
    from app.core.crypto import LocalKmsStub

    with pytest.raises(ValueError) as info:
        LocalKmsStub(key)
    assert key not in str(info.value) or not key


def test_get_key_wrapper_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module reads no Settings and builds no wrapper (CANON.md:163)."""
    import app.core.config as config

    def explode() -> None:
        raise AssertionError("Settings read at import")

    monkeypatch.setattr(config, "get_settings", explode)
    sys.modules.pop("app.core.crypto", None)
    module = importlib.import_module("app.core.crypto")
    assert callable(module.get_key_wrapper)


def test_get_key_wrapper_local_is_stub() -> None:
    from app.core.config import Settings
    from app.core.crypto import LocalKmsStub, get_key_wrapper

    wrapper = get_key_wrapper(Settings(_env_file=None, app_env="local", app_role="shell"))
    assert isinstance(wrapper, LocalKmsStub)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_get_key_wrapper_non_local_not_implemented(app_env: str) -> None:
    from app.core.config import Settings
    from app.core.crypto import get_key_wrapper

    deployed = Settings(_env_file=None, app_env=app_env, app_role="shell")
    with pytest.raises(NotImplementedError, match="Cloud KMS wrapper lands with S4"):
        get_key_wrapper(deployed)


def test_tokenpair_repr_hides() -> None:
    pair = _pair("Zq9-access-secret", "Zq9-refresh-secret")
    rendered = repr(pair) + str(pair) + f"{pair}"
    assert "Zq9" not in rendered
    assert pair.access_token.get_secret_value() == "Zq9-access-secret"


def test_sealed_repr_hides_blob() -> None:
    sealed = _seal(_pair())
    assert sealed.blob.hex() not in repr(sealed)
