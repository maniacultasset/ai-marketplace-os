"""B2 (BE-01; ai-deps C18): the vision model and provider settings and the worker boot rule.

DeepSeek has no vision model, so `LLM_PROVIDER_VISION` accepts `openai` only and defaults to it,
whatever `LLM_PROVIDER` says.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.deployed_env import DEPLOYED_S15_ENV

if TYPE_CHECKING:
    from app.core.config import Settings

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"
DSN = "https://public@o0.ingest.example.invalid/0"
CANARY_KEY = "canary-vision-key-4b1d"
NO_VISION_KEY = (
    "LLM_PROVIDER_VISION=openai requires OPENAI_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)


def _boot(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    from app.core.config import get_settings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
        env.pop("OPENAI_API_KEY")
    if role == "api":
        env["JWT_SECRET"] = "Zq9-not-committed-private-secret-32+"
    return env | extra


def _refusal(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as info:
        _boot(monkeypatch, **env)
    assert isinstance(info.value.code, str)
    return info.value.code


def test_defaults_are_openai_gpt_4o_mini() -> None:
    from app.core.config import Settings

    cfg = Settings(_env_file=None, app_env="local")
    assert (cfg.llm_provider_vision, cfg.llm_model_vision) == ("openai", "gpt-4o-mini")


def test_vision_provider_stays_openai_under_deepseek() -> None:
    from app.core.config import Settings

    cfg = Settings(_env_file=None, app_env="local", llm_provider="deepseek")
    assert cfg.llm_provider_vision == "openai"


def test_env_overrides_the_vision_model(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _boot(monkeypatch, **_env("local", "worker", LLM_MODEL_VISION="gpt-4o"))
    assert (cfg.llm_model_vision, cfg.llm_model_default) == ("gpt-4o", "gpt-4o-mini")


def test_deepseek_vision_provider_refused(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = _refusal(monkeypatch, _env("local", "worker", LLM_PROVIDER_VISION="deepseek"))
    assert "LLM_PROVIDER_VISION (literal_error)" in message
    assert "deepseek" not in message


@pytest.mark.parametrize("model", ["", "x" * 129])
def test_blank_or_long_vision_model_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    message = _refusal(monkeypatch, _env("local", "worker", LLM_MODEL_VISION=model))
    assert "LLM_MODEL_VISION (" in message


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_boot_worker_without_vision_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    assert NO_VISION_KEY in _refusal(monkeypatch, _env(app_env, "worker"))


def test_boot_worker_placeholder_key_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env("staging", "worker", OPENAI_API_KEY="sk-REPLACE_ME")
    assert NO_VISION_KEY in _refusal(monkeypatch, env)


def test_boot_worker_deepseek_key_alone_refused(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generation on DeepSeek still sends images to OpenAI: the DeepSeek key is not enough."""
    env = _env("staging", "worker", LLM_PROVIDER="deepseek", DEEPSEEK_API_KEY=CANARY_KEY)
    assert NO_VISION_KEY in _refusal(monkeypatch, env)


def test_boot_worker_with_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, **_env("staging", "worker", OPENAI_API_KEY=CANARY_KEY)) is not None


def test_boot_worker_local_without_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _boot(monkeypatch, **_env("local", "worker")) is not None


def test_boot_api_staging_without_key_ok(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the worker calls a model; the api boots without a vision key."""
    assert _boot(monkeypatch, **_env("staging", "api")) is not None


def test_vision_refusal_names_no_value(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _env("staging", "worker", OPENAI_API_KEY=f"{CANARY_KEY}REPLACE_ME")
    env["LLM_MODEL_VISION"] = "canary-vision-model"
    message = _refusal(monkeypatch, env)
    assert NO_VISION_KEY in message
    assert CANARY_KEY not in message and "canary-vision-model" not in message
