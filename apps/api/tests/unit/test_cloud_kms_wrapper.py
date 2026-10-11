"""S4 (ADR-0018): the Cloud KMS key wrapper and unwrapper behind the envelope's two protocols.

A fake KMS client stands in for `KeyManagementServiceAsyncClient` (no network, no credentials):
it seals with AES-GCM under its own key and checks the AAD the way KMS does. The seal and unseal
run through the real `app.core.crypto` and `app.adapters.credentials` APIs."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.api_core import exceptions as gexc
from pydantic import SecretStr

KEY = "projects/aimos-staging/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key"
OTHER_KEY = (
    "projects/aimos-prod/locations/asia-south1/keyRings/marketplace-os/cryptoKeys/oauth-token-key"
)
TENANT = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER = uuid.UUID("99999999-2222-4333-8444-555555555555")


def _google_error(kind: type[gexc.GoogleAPICallError], message: str) -> Exception:
    """A google-api-core error, typed as a plain `Exception` for the fake to raise."""
    error: Exception = kind(message)
    return error


class FakeKms:
    """Encrypt and decrypt like Cloud KMS: the primary version wraps, any version unwraps, and a
    wrong AAD is `InvalidArgument`. `fail` makes the next call raise that error instead."""

    def __init__(self, primary: int = 3) -> None:
        self.primary = primary
        self._keys = {primary: AESGCM(AESGCM.generate_key(256))}
        self.encrypts: list[dict[str, Any]] = []
        self.decrypts: list[dict[str, Any]] = []
        self.fail: Exception | None = None
        self.reply_name: str | None = None

    def rotate(self) -> None:
        self.primary += 1
        self._keys[self.primary] = AESGCM(AESGCM.generate_key(256))

    async def encrypt(self, *, request: dict[str, Any], **options: Any) -> SimpleNamespace:
        assert options["timeout"] > 0
        self.encrypts.append(request)
        if self.fail is not None:
            raise self.fail
        nonce = os.urandom(12)
        sealed = self._keys[self.primary].encrypt(
            nonce, request["plaintext"], request["additional_authenticated_data"]
        )
        name = self.reply_name or f"{request['name']}/cryptoKeyVersions/{self.primary}"
        return SimpleNamespace(name=name, ciphertext=bytes([self.primary]) + nonce + sealed)

    async def decrypt(self, *, request: dict[str, Any], **options: Any) -> SimpleNamespace:
        assert options["timeout"] > 0
        self.decrypts.append(request)
        if self.fail is not None:
            raise self.fail
        blob = request["ciphertext"]
        try:
            plaintext = self._keys[blob[0]].decrypt(
                blob[1:13], blob[13:], request["additional_authenticated_data"]
            )
        except (InvalidTag, KeyError):
            raise _google_error(gexc.InvalidArgument, "Decryption failed.") from None
        return SimpleNamespace(plaintext=plaintext)


def _pair(access: str = "Zq9-access-token", refresh: str = "Zq9-refresh-token") -> Any:
    from app.core.crypto import TokenPair

    return TokenPair(access_token=SecretStr(access), refresh_token=SecretStr(refresh))


async def _seal(fake: FakeKms, tenant: uuid.UUID = TENANT) -> Any:
    from app.adapters.kms import CloudKmsKeyWrapper
    from app.core.crypto import seal

    return await seal(_pair(), tenant_id=tenant, wrapper=CloudKmsKeyWrapper(KEY, client=fake))


async def _unseal(fake: FakeKms, sealed: Any, tenant: uuid.UUID = TENANT) -> Any:
    from app.adapters.credentials import CloudKmsKeyUnwrapper, unseal

    return await unseal(
        sealed.blob,
        key_version=sealed.key_version,
        tenant_id=tenant,
        unwrapper=CloudKmsKeyUnwrapper(KEY, client=fake),
    )


async def test_round_trip_through_the_crypto_api() -> None:
    from app.core.crypto import wrap_aad

    fake = FakeKms()
    sealed = await _seal(fake)
    opened = await _unseal(fake, sealed)
    assert opened.access_token.get_secret_value() == "Zq9-access-token"
    assert opened.refresh_token.get_secret_value() == "Zq9-refresh-token"
    (wrap,) = fake.encrypts
    (unwrap,) = fake.decrypts
    assert wrap["name"] == KEY and unwrap["name"] == KEY
    assert wrap["additional_authenticated_data"] == wrap_aad(KEY, TENANT)
    assert unwrap["additional_authenticated_data"] == wrap_aad(KEY, TENANT)
    assert len(wrap["plaintext"]) == 32


async def test_other_tenant_fails() -> None:
    from app.adapters.credentials import CredentialUnsealError

    fake = FakeKms()
    sealed = await _seal(fake)
    with pytest.raises(CredentialUnsealError):
        await _unseal(fake, sealed, tenant=OTHER)
    (unwrap,) = fake.decrypts
    assert unwrap["additional_authenticated_data"].endswith(OTHER.bytes)


async def test_key_version_recorded_and_rotation_still_opens() -> None:
    fake = FakeKms(primary=3)
    old = await _seal(fake)
    assert old.key_version == f"{KEY}/cryptoKeyVersions/3"
    fake.rotate()
    new = await _seal(fake)
    assert new.key_version == f"{KEY}/cryptoKeyVersions/4"
    for sealed in (old, new):
        opened = await _unseal(fake, sealed)
        assert opened.access_token.get_secret_value() == "Zq9-access-token"


async def test_unwrap_refuses_a_version_of_another_key() -> None:
    from dataclasses import replace

    from app.adapters.credentials import CredentialUnsealError

    fake = FakeKms()
    sealed = await _seal(fake)
    for version in (f"{OTHER_KEY}/cryptoKeyVersions/3", "local-1", KEY, ""):
        with pytest.raises(CredentialUnsealError):
            await _unseal(fake, replace(sealed, key_version=version))
    assert fake.decrypts == []


async def test_wrap_refuses_a_reply_from_another_key() -> None:
    from app.adapters.kms import CloudKmsKeyWrapper

    fake = FakeKms()
    fake.reply_name = f"{OTHER_KEY}/cryptoKeyVersions/1"
    with pytest.raises(ValueError, match=r"^key wrap failed$"):
        await CloudKmsKeyWrapper(KEY, client=fake).wrap(b"\x01" * 32, tenant_id=TENANT)


async def test_kms_error_on_wrap_is_detail_free() -> None:
    from app.adapters.kms import CloudKmsKeyWrapper

    fake = FakeKms()
    fake.fail = _google_error(gexc.PermissionDenied, f"Permission denied on {KEY} Zq9-detail")
    dek = b"\x5a" * 32
    with pytest.raises(ValueError) as caught:
        await CloudKmsKeyWrapper(KEY, client=fake).wrap(dek, tenant_id=TENANT)
    exc = caught.value
    assert exc.args == ("key wrap failed",)
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    for rendered in (str(exc), repr(exc)):
        assert "Zq9" not in rendered and dek.hex() not in rendered and "Z" * 32 not in rendered


async def test_kms_error_on_unwrap_is_credential_unseal_error() -> None:
    from app.adapters.credentials import CloudKmsKeyUnwrapper, CredentialUnsealError

    fake = FakeKms()
    sealed = await _seal(fake)
    fake.fail = _google_error(gexc.ServiceUnavailable, "kms down Zq9-detail")
    with pytest.raises(CredentialUnsealError) as caught:
        await _unseal(fake, sealed)
    assert caught.value.args == ("unseal failed",)
    assert caught.value.__cause__ is None
    with pytest.raises(ValueError) as direct:
        await CloudKmsKeyUnwrapper(KEY, client=fake).unwrap(
            b"\x03" * 40, key_version=f"{KEY}/cryptoKeyVersions/3", tenant_id=TENANT
        )
    assert direct.value.args == ("unseal failed",)
    assert direct.value.__context__ is None or direct.value.__suppress_context__


async def test_client_created_lazily_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """No client at import or construction; the first wrap builds one, everything reuses it."""
    import importlib

    from google.cloud import kms

    created: list[FakeKms] = []

    def factory(**kwargs: Any) -> FakeKms:
        assert "credentials" not in kwargs  # Application Default Credentials only
        created.append(FakeKms())
        return created[-1]

    monkeypatch.setattr(kms, "KeyManagementServiceAsyncClient", factory)
    sys.modules.pop("app.adapters.kms", None)
    module = importlib.import_module("app.adapters.kms")
    from app.adapters.credentials import CloudKmsKeyUnwrapper

    wrapper = module.CloudKmsKeyWrapper(KEY)
    unwrapper = CloudKmsKeyUnwrapper(KEY)
    assert created == []
    wrapped, version = await wrapper.wrap(b"\x01" * 32, tenant_id=TENANT)
    await wrapper.wrap(b"\x02" * 32, tenant_id=TENANT)
    dek = await unwrapper.unwrap(wrapped, key_version=version, tenant_id=TENANT)
    assert dek == b"\x01" * 32
    assert len(created) == 1
    assert len(created[0].encrypts) == 2 and len(created[0].decrypts) == 1


def test_importing_the_envelope_loads_no_kms_client() -> None:
    """CANON.md:163: the envelope modules import neither the KMS SDK nor its adapter."""
    api_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys, app.core.crypto, app.adapters.credentials\n"
        "loaded = [m for m in ('google.cloud.kms', 'app.adapters.kms') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    env = {**os.environ, "PYTHONPATH": str(api_root)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=api_root, timeout=60)


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_settings_select_cloud_kms(app_env: str) -> None:
    from app.adapters.credentials import CloudKmsKeyUnwrapper, get_key_unwrapper
    from app.adapters.kms import CloudKmsKeyWrapper
    from app.core.config import Settings
    from app.core.crypto import get_key_wrapper

    deployed = Settings(_env_file=None, app_env=app_env, app_role="worker", kms_key_uri=KEY)
    wrapper = get_key_wrapper(deployed)
    unwrapper = get_key_unwrapper(deployed)
    assert isinstance(wrapper, CloudKmsKeyWrapper) and wrapper.key_uri == KEY
    assert isinstance(unwrapper, CloudKmsKeyUnwrapper) and unwrapper.key_uri == KEY


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deployed_settings_without_key_uri_refuse(app_env: str) -> None:
    from app.adapters.credentials import get_key_unwrapper
    from app.core.config import Settings
    from app.core.crypto import get_key_wrapper

    deployed = Settings(_env_file=None, app_env=app_env, app_role="shell")
    with pytest.raises(RuntimeError, match="KMS_KEY_URI"):
        get_key_wrapper(deployed)
    with pytest.raises(RuntimeError, match="KMS_KEY_URI"):
        get_key_unwrapper(deployed)


def test_local_settings_select_the_stubs() -> None:
    from app.adapters.credentials import LocalKmsUnwrapStub, get_key_unwrapper
    from app.core.config import Settings
    from app.core.crypto import LocalKmsStub, get_key_wrapper

    local = Settings(_env_file=None, app_env="local", app_role="worker", kms_key_uri=KEY)
    assert isinstance(get_key_wrapper(local), LocalKmsStub)
    assert isinstance(get_key_unwrapper(local), LocalKmsUnwrapStub)
