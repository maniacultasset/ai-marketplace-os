"""ADR-0024: the model gateway (`LLM_GATEWAY`) and the worker boot rule for the model keys.

`direct` (the default) is the S16/B2 behaviour: OPENAI_API_KEY for embedding and vision, the
`LLM_PROVIDER` key for generation. `openrouter` sends every model call to the pinned
OPENROUTER_BASE_URL with one key, OPENROUTER_API_KEY, and a worker off local needs that key and
no other. The base URL is a constant: no setting can name another host.
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
CANARY_KEY = "canary-gateway-key"
MODEL_KEYS = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
NO_DEEPSEEK_KEY = (
    "LLM_PROVIDER=deepseek requires DEEPSEEK_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)
NO_OPENAI_GEN_KEY = (
    "LLM_PROVIDER=openai requires OPENAI_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)
NO_ROUTER_KEY = (
    "LLM_GATEWAY=openrouter requires OPENROUTER_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    from app.core.config import get_settings

    for name in (*MODEL_KEYS, "LLM_GATEWAY", "LLM_PROVIDER"):
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


def _router_env(app_env: str, role: str = "worker", **extra: str) -> dict[str, str]:
    """A deployed env under openrouter: no OpenAI key at all (DEPLOYED_S15_ENV carries one)."""
    env = _env(app_env, role, LLM_GATEWAY="openrouter", **extra)
    env.pop("OPENAI_API_KEY", None)
    return env


def _refusal(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


# ---- LLM_GATEWAY: the setting and the pinned base ----


def test_default_is_direct() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None, app_env="local").llm_gateway == "direct"


@pytest.mark.parametrize("value", ["", "OpenRouter", "https://openrouter.ai/api/v1", "openai"])
def test_unknown_gateway_refused(value: str) -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="local", llm_gateway=value)


def test_no_setting_names_a_base_url() -> None:
    """The residual SSRF gap of a free-form base is closed: no field carries a model host."""
    from app.core.config import Settings

    assert "llm_api_base" not in Settings.model_fields
    assert not [name for name in Settings.model_fields if "base" in name and "llm" in name]


def test_openrouter_base_is_a_seam_valid_constant() -> None:
    from app.core.config import OPENROUTER_BASE_URL

    assert OPENROUTER_BASE_URL == OPENROUTER
    creds = ModelCredentials(provider="deepseek", api_key="k", api_base=OPENROUTER, model="m")
    assert creds.api_base == OPENROUTER


def test_openrouter_settings_load(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The values prod.env carries boot a production worker with the one key."""
    env = _router_env(
        "production",
        LLM_PROVIDER="deepseek",
        LLM_MODEL_DEFAULT="deepseek/deepseek-chat",
        OPENROUTER_API_KEY=CANARY_KEY,
        LLM_DAILY_BUDGET_USD="3",
        LLM_TENANT_DAILY_BUDGET_USD="1",
    )
    monkeypatch.setattr("app.core.config._pipeline_stub_installed", lambda: False)
    cfg = _boot(monkeypatch, **env)
    assert (cfg.llm_gateway, cfg.llm_provider, cfg.llm_model_default) == (
        "openrouter",
        "deepseek",
        "deepseek/deepseek-chat",
    )
    assert cfg.openrouter_api_key == CANARY_KEY and cfg.openai_api_key is None
    assert (cfg.llm_daily_budget_usd, cfg.llm_tenant_daily_budget_usd) == (3.0, 1.0)
    assert CANARY_KEY not in repr(cfg)


# ---- openrouter: one key at worker boot ----


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_router_worker_needs_only_the_router_key(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str, provider: str
) -> None:
    """No OPENAI_API_KEY and no DEEPSEEK_API_KEY: the router key is enough, for any provider."""
    monkeypatch.setattr("app.core.config._pipeline_stub_installed", lambda: False)
    env = _router_env(app_env, LLM_PROVIDER=provider, OPENROUTER_API_KEY=CANARY_KEY)
    assert _boot(monkeypatch, **env).llm_gateway == "openrouter"


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_router_worker_without_its_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    monkeypatch.setattr("app.core.config._pipeline_stub_installed", lambda: False)
    message = _refusal(monkeypatch, _router_env(app_env, LLM_PROVIDER="deepseek"))
    assert NO_ROUTER_KEY in message
    # the direct keys are not asked for under openrouter
    assert "OPENAI_API_KEY" not in message and "DEEPSEEK_API_KEY" not in message


@pytest.mark.parametrize("key", ["", "   ", "sk-REPLACE_ME"])
def test_router_worker_blank_or_placeholder_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    env = _router_env("staging", OPENROUTER_API_KEY=key)
    assert NO_ROUTER_KEY in _refusal(monkeypatch, env)


def test_router_key_does_not_stand_in_under_direct(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("staging", "worker", OPENROUTER_API_KEY=CANARY_KEY)
    env.pop("OPENAI_API_KEY")
    message = _refusal(monkeypatch, env)
    assert "APP_ROLE=worker requires OPENAI_API_KEY outside APP_ENV=local" in message
    assert NO_OPENAI_GEN_KEY in message and NO_ROUTER_KEY not in message


def test_router_local_worker_and_deployed_api_need_no_key(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _boot(monkeypatch, **_router_env("local")) is not None
    assert _boot(monkeypatch, **_router_env("staging", "api")) is not None


def test_router_refusal_names_no_value(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _router_env(
        "staging",
        OPENROUTER_API_KEY=f"{CANARY_KEY}REPLACE_ME",
        LLM_MODEL_DEFAULT="canary-generation-model",
    )
    message = _refusal(monkeypatch, env)
    assert NO_ROUTER_KEY in message
    assert CANARY_KEY not in message and "canary-generation-model" not in message


# ---- direct: the generation provider's key at worker boot (unchanged contract) ----


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_deepseek_worker_without_its_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    env = _env(app_env, "worker", LLM_PROVIDER="deepseek")
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


def test_direct_local_worker_and_deployed_api_need_no_generation_key(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _boot(monkeypatch, **_env("local", "worker", LLM_PROVIDER="deepseek")) is not None
    assert _boot(monkeypatch, **_env("staging", "api", LLM_PROVIDER="deepseek")) is not None


def test_direct_refusal_names_no_value(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
