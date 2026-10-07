"""S17b T1 (DW4, AC4; KNOWN-ISSUES D): the storage client refuses the committed `local_only_`
MinIO values, blank keys and a non-https endpoint outside `APP_ENV=local`, whatever the role,
and its messages never carry a value."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from app.adapters import storage
from app.adapters.storage import StorageConfigError, get_storage, storage_config_errors
from app.core.config import Settings
from tests.support.deployed_env import DEPLOYED_S15_ENV

LOCAL_USER = "local_only_minio_root_user"
LOCAL_SECRET = "local_only_minio_root_password_32_chars"
SECRET = "Zq9-deployed-secret-key-not-committed"


def _settings(app_env: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": app_env,
        "s3_endpoint_url": DEPLOYED_S15_ENV["S3_ENDPOINT_URL"],
        "s3_access_key": DEPLOYED_S15_ENV["S3_ACCESS_KEY"],
        "s3_secret_key": DEPLOYED_S15_ENV["S3_SECRET_KEY"],
        **overrides,
    }
    # model_construct: the guard is pure over the values, the boot rules are another test's
    return Settings.model_construct(**values)


@pytest.fixture
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    from app.core.config import get_settings

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_storage.cache_clear()
    yield monkeypatch
    get_storage.cache_clear()
    get_settings.cache_clear()


def _variant(value: str, form: str) -> str:
    return {"plain": value, "UPPER": value.upper(), "padded": f"  {value}\t"}[form]


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("which", ["user", "secret"])
@pytest.mark.parametrize("form", ["plain", "UPPER", "padded"])
def test_storage_rejects_local_values(app_env: str, which: str, form: str) -> None:
    field, value = (
        ("s3_access_key", LOCAL_USER) if which == "user" else ("s3_secret_key", LOCAL_SECRET)
    )
    errors = storage_config_errors(_settings(app_env, **{field: _variant(value, form)}))
    assert errors == ["S3 credentials are local-only values"]


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_storage_refuses_http_outside_local(app_env: str) -> None:
    errors = storage_config_errors(_settings(app_env, s3_endpoint_url="http://storage.example"))
    assert errors == ["S3_ENDPOINT_URL must use https outside APP_ENV=local"]


def test_storage_refuses_default_endpoint_outside_local() -> None:
    errors = storage_config_errors(_settings("staging", s3_endpoint_url="http://localhost:9000"))
    assert "S3_ENDPOINT_URL must use https outside APP_ENV=local" in errors


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("field", ["s3_access_key", "s3_secret_key"])
def test_storage_refuses_missing_keys(blank: str, field: str) -> None:
    errors = storage_config_errors(_settings("staging", **{field: blank}))
    assert errors == ["S3_ACCESS_KEY and S3_SECRET_KEY must be set outside APP_ENV=local"]


def test_storage_deployed_values_accepted() -> None:
    assert storage_config_errors(_settings("production")) == []


def test_storage_local_accepts_defaults(fresh: pytest.MonkeyPatch) -> None:
    """The committed defaults (`config.py`) under `APP_ENV=local` build a client."""
    fresh.setenv("APP_ENV", "local")
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        fresh.delenv(name, raising=False)
    from app.core.config import get_settings

    assert storage_config_errors(get_settings()) == []
    assert get_storage() is not None


def test_storage_error_has_no_value(fresh: pytest.MonkeyPatch) -> None:
    """`get_storage()` raises before any client is built; message and repr carry no value."""
    from app.core.config import get_settings

    built: list[Any] = []
    fresh.setattr(
        storage,
        "get_settings",
        lambda: _settings("staging", s3_access_key=LOCAL_USER, s3_secret_key=LOCAL_SECRET),
    )
    fresh.setattr(boto3.session.Session, "client", lambda *a, **k: built.append(k))
    with pytest.raises(StorageConfigError) as caught:
        get_storage()
    assert built == []
    shown = str(caught.value) + repr(caught.value)
    assert LOCAL_USER not in shown and LOCAL_SECRET not in shown
    assert get_settings is not None


def test_storage_client_explicit_credentials(fresh: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def spy(_self: Any, service: str, **kwargs: Any) -> object:
        seen.update(kwargs, service=service)
        return object()

    fresh.setattr(storage, "get_settings", lambda: _settings("production"))
    fresh.setattr(boto3.session.Session, "client", spy)
    get_storage()
    assert seen["service"] == "s3"
    assert seen["aws_access_key_id"] == DEPLOYED_S15_ENV["S3_ACCESS_KEY"]
    assert seen["aws_secret_access_key"] == SECRET
    assert seen["config"].signature_version == "s3v4"
    assert seen["config"].s3 == {"addressing_style": "path"}
    assert seen["config"].connect_timeout == storage.CONNECT_TIMEOUT_S
    assert seen["config"].read_timeout == storage.READ_TIMEOUT_S
