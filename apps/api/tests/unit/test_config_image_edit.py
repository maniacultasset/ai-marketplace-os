"""SI7 T4: the image-edit settings, the worker boot rule and the image-edit credentials (AC14, AC18).

The edit goes only through OpenRouter, so a deployed worker with the switch on needs the gateway
and a `vendor/model` id. The api never calls the model and keeps booting under `direct`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from tests.support.deployed_env import DEPLOYED_S15_ENV

if TYPE_CHECKING:
    from app.core.config import Settings

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
ROUTER_KEY = "sk-or-test-" + "8" * 32
MODEL_KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
SWITCH_KEYS = (
    "LLM_GATEWAY",
    "LLM_PROVIDER",
    "IMAGE_EDIT_ENABLED",
    "LLM_MODEL_IMAGE_EDIT",
    "IMAGE_EDIT_TENANT_DAILY_LIMIT",
    "IMAGE_EDIT_PLATFORM_DAILY_SHARE",
)
NO_GATEWAY = (
    "IMAGE_EDIT_ENABLED requires LLM_GATEWAY=openrouter for APP_ROLE=worker outside APP_ENV=local"
)
NO_VENDOR = "IMAGE_EDIT_ENABLED requires LLM_MODEL_IMAGE_EDIT as vendor/model"


@pytest.fixture(autouse=True)
def _no_dotenv(clean_env: Path) -> None:
    """Boot from the given env only: the lane's `.env` never leaks in."""


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    from app.core.config import get_settings

    for name in (*MODEL_KEYS, *SWITCH_KEYS):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


def _env(app_env: str, role: str, **extra: str) -> dict[str, str]:
    env = {"APP_ENV": app_env, "APP_ROLE": role, "DATABASE_URL": LOCAL_URL}
    if app_env != "local":
        env |= {"SENTRY_DSN": DSN, **DEPLOYED_S15_ENV}
    if role == "api":
        env["JWT_SECRET"] = "Zq9-not-committed-private-secret-32+"
    return env | extra


def _router(role: str = "worker", **extra: str) -> dict[str, str]:
    env = _env(
        "staging",
        role,
        LLM_GATEWAY="openrouter",
        LLM_MODEL_DEFAULT="deepseek/deepseek-chat",
        OPENROUTER_API_KEY=ROUTER_KEY,
        **extra,
    )
    env.pop("OPENAI_API_KEY", None)
    return env


def _refusal(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


def test_defaults_are_off_and_the_flash_image_model() -> None:
    from app.core.config import Settings

    s = Settings(_env_file=None, app_env="local")
    assert s.image_edit_enabled is False
    assert s.llm_model_image_edit == "google/gemini-2.5-flash-image"
    assert s.image_edit_tenant_daily_limit == 10
    assert s.image_edit_platform_daily_share == Decimal("0.5")


@pytest.mark.parametrize(
    "field",
    [
        {"image_edit_tenant_daily_limit": -1},
        {"image_edit_tenant_daily_limit": 1001},
        {"image_edit_platform_daily_share": Decimal("1.01")},
        {"image_edit_platform_daily_share": Decimal("-0.1")},
        {"image_edit_platform_daily_share": "NaN"},
        {"image_edit_platform_daily_share": "Infinity"},
        {"llm_model_image_edit": ""},
        {"llm_model_image_edit": "v/" + "m" * 127},
    ],
)
def test_bad_values_refused(field: dict[str, object]) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="local", **field)  # type: ignore[arg-type]


def test_staging_worker_direct_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("staging", "worker", IMAGE_EDIT_ENABLED="true")
    assert NO_GATEWAY in _refusal(monkeypatch, env)


def test_staging_worker_without_vendor_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _router(IMAGE_EDIT_ENABLED="true", LLM_MODEL_IMAGE_EDIT="gemini-2.5-flash-image")
    code = _refusal(monkeypatch, env)
    assert NO_VENDOR in code and NO_GATEWAY not in code


def test_staging_worker_openrouter_vendor_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _boot(monkeypatch, **_router(IMAGE_EDIT_ENABLED="true"))
    assert s.image_edit_enabled is True


def test_staging_worker_switch_off_never_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _boot(monkeypatch, **_env("staging", "worker", LLM_MODEL_IMAGE_EDIT="bare"))
    assert s.image_edit_enabled is False


def test_local_direct_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _boot(monkeypatch, **_env("local", "worker", IMAGE_EDIT_ENABLED="true"))
    assert s.image_edit_enabled and s.llm_gateway == "direct"


def test_staging_api_direct_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    """The api answers the create and lists `fixes`; it never calls the model."""
    s = _boot(monkeypatch, **_env("staging", "api", IMAGE_EDIT_ENABLED="true"))
    assert s.image_edit_enabled and s.llm_gateway == "direct"


def test_errors_name_no_key_and_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_model = "secretvendor-model-name"
    env = _env(
        "staging",
        "worker",
        IMAGE_EDIT_ENABLED="true",
        LLM_MODEL_IMAGE_EDIT=secret_model,
        OPENROUTER_API_KEY=ROUTER_KEY,
    )
    code = _refusal(monkeypatch, env)
    assert secret_model not in code and ROUTER_KEY not in code


def test_credentials_under_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import OPENROUTER_BASE_URL
    from app.services.pipeline_deps import image_edit_credentials

    s = _boot(monkeypatch, **_router(IMAGE_EDIT_ENABLED="true"))
    creds = image_edit_credentials(s)
    assert creds.provider == "openai"
    assert creds.api_base == OPENROUTER_BASE_URL
    assert creds.model == "google/gemini-2.5-flash-image"
    assert creds.api_key.get_secret_value() == ROUTER_KEY
    assert ROUTER_KEY not in repr(creds) and ROUTER_KEY not in str(creds)


def test_credentials_local_direct_use_the_placeholder() -> None:
    from app.core.config import Settings
    from app.services.pipeline_deps import LOCAL_PLACEHOLDER_KEY, image_edit_credentials

    creds = image_edit_credentials(Settings(_env_file=None, app_env="local"))
    assert creds.api_key.get_secret_value() == LOCAL_PLACEHOLDER_KEY
    assert creds.api_base is None


def test_credentials_off_local_without_the_gateway_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.pipeline_deps import SeamPortError, image_edit_credentials
    from mkos_schemas.codes import ErrorCode

    s = _boot(monkeypatch, **_env("staging", "worker"))
    with pytest.raises(SeamPortError) as info:
        image_edit_credentials(s)
    assert info.value.code is ErrorCode.MODEL_UNAVAILABLE
