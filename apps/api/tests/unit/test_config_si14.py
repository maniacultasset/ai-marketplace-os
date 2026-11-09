"""SI14 T6: the four V1.1 switches (off by default) and the per-purpose model gateways (D13).

With no `LLM_GATEWAY_<PURPOSE>` set every check gives exactly today's messages (the existing
`test_generation_config.py` cases run unchanged); a per-purpose gateway only adds the checks of
the purposes it routes."""

from __future__ import annotations

from typing import Any

import pytest
from app.core import config
from app.core.config import LLM_PURPOSES, Settings, effective_gateway
from pydantic import ValidationError

SWITCHES = (
    "category_suggest_enabled",
    "column_map_ai_enabled",
    "category_lamps_enabled",
    "photo_qa_enabled",
)
NO_ROUTER_KEY = (
    "LLM_GATEWAY=openrouter requires OPENROUTER_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)
NO_ROUTER_MODEL = "LLM_GATEWAY=openrouter requires LLM_MODEL_DEFAULT as vendor/model"
NO_ROUTER_VISION = "LLM_GATEWAY=openrouter requires LLM_MODEL_VISION as an OpenAI model id"
NO_EMBED_KEY = "APP_ROLE=worker requires OPENAI_API_KEY outside APP_ENV=local"
NO_VISION_KEY = (
    "LLM_PROVIDER_VISION=openai requires OPENAI_API_KEY for APP_ROLE=worker outside APP_ENV=local"
)
NO_GEN_KEY = "LLM_PROVIDER=openai requires OPENAI_API_KEY for APP_ROLE=worker outside APP_ENV=local"


def _worker(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "production",
        "app_role": "worker",
        "openai_api_key": None,
        "openrouter_api_key": None,
    }
    return Settings(_env_file=None, **(base | values))


def _model_errors(s: Settings) -> list[str]:
    return [
        *config._embedding_errors(s),
        *config._vision_errors(s),
        *config._generation_errors(s),
        *config._gateway_errors(s),
    ]


def test_switches_default_off_and_gateways_unset() -> None:
    s = Settings(_env_file=None, app_env="local")
    assert not any(getattr(s, name) for name in SWITCHES)
    assert all(getattr(s, f"llm_gateway_{p}") is None for p in LLM_PURPOSES)
    assert {effective_gateway(s, p) for p in LLM_PURPOSES} == {"direct"}


def test_blank_gateway_is_unset() -> None:
    s = Settings(_env_file=None, app_env="local", llm_gateway_classify="")
    assert s.llm_gateway_classify is None


@pytest.mark.parametrize("value", ["india", "OpenRouter", "https://x.example"])
def test_unknown_purpose_gateway_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="local", llm_gateway_vision=value)


def test_effective_gateway_prefers_the_purpose() -> None:
    s = _worker(llm_gateway="direct", llm_gateway_classify="openrouter")
    assert effective_gateway(s, "classify") == "openrouter"
    assert effective_gateway(s, "vision") == "direct"


def test_no_purpose_setting_keeps_todays_direct_messages() -> None:
    assert _model_errors(_worker()) == [NO_EMBED_KEY, NO_VISION_KEY, NO_GEN_KEY]


def test_no_purpose_setting_keeps_todays_openrouter_messages() -> None:
    assert _model_errors(_worker(llm_gateway="openrouter")) == [NO_ROUTER_KEY, NO_ROUTER_MODEL]


def test_classify_on_openrouter_under_direct_needs_the_router_key() -> None:
    errors = _model_errors(_worker(llm_gateway_classify="openrouter", openai_api_key="sk-real"))
    assert errors == [NO_ROUTER_KEY]  # the classify (vision) model is checked, the default is not
    errors = _model_errors(
        _worker(
            llm_gateway_classify="openrouter",
            openai_api_key="sk-real",
            llm_model_vision="openai/gpt-4o-mini",
        )
    )
    assert errors == [NO_ROUTER_KEY, NO_ROUTER_VISION]


def test_embedding_direct_under_openrouter_needs_the_openai_key() -> None:
    errors = _model_errors(
        _worker(
            llm_gateway="openrouter",
            llm_gateway_embedding="direct",
            openrouter_api_key="or-real-key",
            llm_model_default="deepseek/deepseek-chat",
        )
    )
    assert errors == [NO_EMBED_KEY]


def test_columns_direct_under_openrouter_needs_the_generation_key() -> None:
    errors = _model_errors(
        _worker(
            llm_gateway="openrouter",
            llm_gateway_columns="direct",
            openrouter_api_key="or-real-key",
            llm_model_default="deepseek/deepseek-chat",
        )
    )
    assert errors == [NO_GEN_KEY]


def test_messages_carry_names_only() -> None:
    s = _worker(llm_gateway_classify="openrouter", openai_api_key="sk-secret-value-123")
    assert all("sk-secret" not in message for message in _model_errors(s))
