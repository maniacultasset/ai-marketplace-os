"""ADR-0024: the generation route (`LLM_API_BASE`) and the worker boot rule for the generation
provider's key.

LLM_API_BASE follows the seam's own `ModelCredentials.api_base` rule in every env, so a base the
pipeline would refuse never boots. A worker off local refuses to start without the key of
`LLM_PROVIDER` (OpenRouter is `deepseek` plus the base, with its key in DEEPSEEK_API_KEY).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mkos_schemas.jobs import ModelCredentials
from pydantic import ValidationError
from tests.support.deployed_env import DEPLOYED_S15_ENV

if TYPE_CHECKING:
    from app.core.config import Settings

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
OPENROUTER = "https://openrouter.ai/api/v1"
CANARY_KEY = "canary-generation-key"
NO_DEEPSEEK_KEY = (
    "LLM_PROVIDER=deepseek requires DEEPSEEK_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)
NO_OPENAI_GEN_KEY = (
    "LLM_PROVIDER=openai requires OPENAI_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)

ACCEPTED = [
    OPENROUTER,
    "https://api.deepseek.com/v1",
    "https://api.openai.com/v1",
    "https://8.8.8.8/v1",
    "https://[2001:4860:4860::8888]/v1",
    "https://router.example.com:8443/api/v1",
]
REFUSED = [
    "http://openrouter.ai/api/v1",
    "ftp://openrouter.ai/",
    "openrouter.ai/api/v1",
    "https://user:pw@openrouter.ai/api/v1",
    "https://user@openrouter.ai/api/v1",
    "https:///api/v1",
    "https://localhost/v1",
    "https://127.0.0.1/v1",
    "https://169.254.169.254/latest",
    "https://10.0.0.5/v1",
    "https://192.168.1.10/v1",
    "https://172.16.0.1/v1",
    "https://[::1]/v1",
    "https://[fe80::1]/v1",
    "https://" + "a" * 2040 + ".com/",
]


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    from app.core.config import get_settings

    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_BASE", "LLM_PROVIDER"):
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


def _refusal(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


def _schema_accepts(base: str) -> bool:
    try:
        ModelCredentials(provider="deepseek", api_key="k", api_base=base, model="m")
    except ValidationError:
        return False
    return True


def _settings_accepts(base: str) -> bool:
    from app.core.config import Settings

    try:
        Settings(_env_file=None, app_env="local", llm_api_base=base)
    except ValidationError:
        return False
    return True


# ---- LLM_API_BASE: the seam's rule ----


def test_default_is_none() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None, app_env="local").llm_api_base is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_is_none(blank: str) -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None, app_env="local", llm_api_base=blank).llm_api_base is None


@pytest.mark.parametrize("base", ACCEPTED)
def test_accepted_bases_match_the_schema(base: str) -> None:
    assert _schema_accepts(base) and _settings_accepts(base)


@pytest.mark.parametrize("base", REFUSED)
def test_refused_bases_match_the_schema(base: str) -> None:
    """The boot refuses exactly what ModelCredentials refuses, so no job fails on a bad base."""
    assert not _schema_accepts(base)
    assert not _settings_accepts(base)


@pytest.mark.parametrize("app_env", ["local", "staging", "production"])
def test_bad_base_refused_in_every_env_without_its_value(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    base = "http://canary-base.example.invalid/v1"
    env = _env(app_env, "worker", LLM_API_BASE=base, OPENAI_API_KEY=CANARY_KEY)
    message = _refusal(monkeypatch, env)
    assert "LLM_API_BASE (value_error)" in message
    assert "canary-base" not in message and CANARY_KEY not in message


def test_openrouter_settings_load(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env-file values ENV.md documents for OpenRouter boot a deployed worker."""
    env = _env(
        "production",
        "worker",
        LLM_PROVIDER="deepseek",
        LLM_MODEL_DEFAULT="deepseek/deepseek-chat",
        LLM_API_BASE=OPENROUTER,
        DEEPSEEK_API_KEY=CANARY_KEY,
        LLM_DAILY_BUDGET_USD="3",
        LLM_TENANT_DAILY_BUDGET_USD="1",
    )
    monkeypatch.setattr("app.core.config._pipeline_stub_installed", lambda: False)
    cfg = _boot(monkeypatch, **env)
    assert (cfg.llm_provider, cfg.llm_model_default, cfg.llm_api_base) == (
        "deepseek",
        "deepseek/deepseek-chat",
        OPENROUTER,
    )
    assert (cfg.llm_daily_budget_usd, cfg.llm_tenant_daily_budget_usd) == (3.0, 1.0)


# ---- the generation provider's key at worker boot ----


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deepseek_worker_without_its_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    env = _env(app_env, "worker", LLM_PROVIDER="deepseek", LLM_API_BASE=OPENROUTER)
    assert NO_DEEPSEEK_KEY in _refusal(monkeypatch, env)


@pytest.mark.parametrize("key", ["", "   ", "sk-REPLACE_ME"])
def test_deepseek_worker_blank_or_placeholder_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    env = _env("staging", "worker", LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY=key)
    assert NO_DEEPSEEK_KEY in _refusal(monkeypatch, env)


def test_deepseek_worker_with_its_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("staging", "worker", LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY=CANARY_KEY)
    assert _boot(monkeypatch, **env).llm_provider == "deepseek"


def test_openai_worker_without_key_names_the_generation_provider(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("staging", "worker")
    env.pop("OPENAI_API_KEY")
    assert NO_OPENAI_GEN_KEY in _refusal(monkeypatch, env)


def test_local_worker_and_deployed_api_need_no_generation_key(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _boot(monkeypatch, **_env("local", "worker", LLM_PROVIDER="deepseek")) is not None
    assert _boot(monkeypatch, **_env("staging", "api", LLM_PROVIDER="deepseek")) is not None


def test_generation_refusal_names_no_value(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(
        "staging",
        "worker",
        LLM_PROVIDER="deepseek",
        DEEPSEEK_API_KEY=f"{CANARY_KEY}REPLACE_ME",
        LLM_MODEL_DEFAULT="canary-generation-model",
    )
    message = _refusal(monkeypatch, env)
    assert NO_DEEPSEEK_KEY in message
    assert CANARY_KEY not in message and "canary-generation-model" not in message
