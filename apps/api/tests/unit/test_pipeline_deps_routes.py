"""SI14 T7: per-purpose credential routes. With no `LLM_GATEWAY_<PURPOSE>` set, every purpose's
credentials (and the size-chart and edit ones) equal what the pre-SI14 code gave; a per-purpose
gateway moves only its own call."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from app.core.config import OPENROUTER_BASE_URL, Settings, gateway_openai_model
from app.services.pipeline_deps import (
    LOCAL_PLACEHOLDER_KEY,
    SeamPortError,
    _ModelConfig,
    classify_credentials,
    column_map_credentials,
    credentials_for,
    image_edit_credentials,
    size_chart_read_credentials,
)
from mkos_schemas.jobs import ModelCredentials

KEYS = {"openai_api_key": "sk-openai-k", "deepseek_api_key": "ds-k", "openrouter_api_key": "or-k"}


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **({"app_env": "local"} | KEYS | values))


def _pre_si14(s: Settings, purpose: str) -> ModelCredentials:
    """The credentials the code gave before SI14 (`_ModelConfig.from_settings` as of 4ce40d32)."""
    if s.llm_gateway == "openrouter":
        key, base = s.openrouter_api_key, OPENROUTER_BASE_URL
        vision = gateway_openai_model(s.llm_model_vision)
        embedding = gateway_openai_model(s.embedding_model)
        gen_key = vision_key = embed_key = key
    else:
        base, vision, embedding = None, s.llm_model_vision, s.embedding_model
        gen_key = s.openai_api_key if s.llm_provider == "openai" else s.deepseek_api_key
        vision_key = embed_key = s.openai_api_key
    provider: Any
    if purpose == "embedding":
        provider, key, model = "openai", embed_key, embedding
    elif purpose == "vision":
        provider, key, model = s.llm_provider_vision, vision_key, vision
    else:
        provider, key, model = s.llm_provider, gen_key, s.llm_model_default
    return ModelCredentials(
        provider=provider,
        api_key=key or LOCAL_PLACEHOLDER_KEY,
        api_base=base,
        model=model,
        timeout_seconds=float(s.llm_timeout_seconds),
    )


GRID = [
    {"llm_gateway": gateway, "llm_provider": provider, "llm_model_default": model}
    for gateway, provider, model in itertools.product(
        ("direct", "openrouter"), ("openai", "deepseek"), ("gpt-4o-mini", "deepseek/deepseek-chat")
    )
]


def _same(a: ModelCredentials, b: ModelCredentials) -> bool:
    return a.model_dump() == b.model_dump() and (
        a.api_key.get_secret_value() == b.api_key.get_secret_value()
    )


@pytest.mark.parametrize("values", GRID)
def test_default_routes_unchanged(values: dict[str, Any]) -> None:
    s = _settings(**values)
    models = _ModelConfig.from_settings(s)
    for purpose in ("generation", "vision", "embedding"):
        assert _same(credentials_for(models, purpose), _pre_si14(s, purpose)), purpose
    # with no per-purpose gateway the two new calls ride their routes unchanged
    assert _same(classify_credentials(s), _pre_si14(s, "vision"))
    assert _same(column_map_credentials(s), _pre_si14(s, "generation"))


@pytest.mark.parametrize("gateway", ["direct", "openrouter"])
def test_size_chart_and_edit_unchanged(gateway: str) -> None:
    s = _settings(llm_gateway=gateway, llm_model_vision_size_chart="gpt-4o")
    read = size_chart_read_credentials(s)
    assert read.model == ("openai/gpt-4o" if gateway == "openrouter" else "gpt-4o")
    assert read.api_base == (OPENROUTER_BASE_URL if gateway == "openrouter" else None)
    edit = image_edit_credentials(s)
    assert edit.model == s.llm_model_image_edit


def test_classify_gateway_moves_only_classify() -> None:
    base = _settings(llm_gateway="direct")
    routed = _settings(llm_gateway="direct", llm_gateway_classify="openrouter")
    assert classify_credentials(routed).api_base == OPENROUTER_BASE_URL
    assert classify_credentials(routed).model == gateway_openai_model(routed.llm_model_vision)
    assert classify_credentials(routed).api_key.get_secret_value() == "or-k"
    for purpose in ("generation", "vision", "embedding"):
        assert _same(
            credentials_for(_ModelConfig.from_settings(routed), purpose),
            credentials_for(_ModelConfig.from_settings(base), purpose),
        )
    assert _same(column_map_credentials(routed), column_map_credentials(base))


def test_columns_gateway_moves_only_columns() -> None:
    routed = _settings(
        llm_gateway="openrouter", llm_gateway_columns="direct", llm_model_default="gpt-4o-mini"
    )
    creds = column_map_credentials(routed)
    assert creds.api_base is None and creds.api_key.get_secret_value() == "sk-openai-k"
    assert classify_credentials(routed).api_base == OPENROUTER_BASE_URL


def test_missing_key_outside_local_is_model_unavailable() -> None:
    s = _settings(
        app_env="staging",
        openrouter_api_key=None,
        llm_gateway_classify="openrouter",
        jwt_secret="Zq9-not-committed-private-secret-32+",
    )
    with pytest.raises(SeamPortError) as caught:
        classify_credentials(s)
    assert caught.value.code.value == "MODEL_UNAVAILABLE"


def test_key_never_in_repr() -> None:
    s = _settings(llm_gateway_columns="openrouter")
    models = _ModelConfig.from_settings(s)
    for text in (repr(models), repr(column_map_credentials(s)), repr(classify_credentials(s))):
        assert "or-k" not in text and "sk-openai-k" not in text


@pytest.mark.parametrize("values", GRID)
def test_text_classify_rides_the_generation_route(values: dict[str, Any]) -> None:
    """`CATEGORY_SUGGEST_MODEL=text`: the classifier gets exactly the generation credentials; the
    other purposes do not move."""
    base = _settings(**values)
    s = _settings(**values, category_suggest_model="text")
    assert _same(classify_credentials(s), _pre_si14(s, "generation"))
    assert _same(classify_credentials(s), column_map_credentials(s))
    for purpose in ("generation", "vision", "embedding"):
        assert _same(
            credentials_for(_ModelConfig.from_settings(s), purpose),
            credentials_for(_ModelConfig.from_settings(base), purpose),
        )


def test_text_classify_keeps_its_own_gateway() -> None:
    s = _settings(
        llm_gateway="direct",
        llm_gateway_classify="openrouter",
        category_suggest_model="text",
        llm_model_default="deepseek/deepseek-v4-flash",
    )
    creds = classify_credentials(s)
    assert creds.api_base == OPENROUTER_BASE_URL and creds.model == "deepseek/deepseek-v4-flash"
    assert creds.api_key.get_secret_value() == "or-k"


def test_per_call_timeout_is_the_spec_40_s() -> None:
    """LEFTOVERS 4.10: every text, vision and embedding call gets 40 s by default (the AI spec's
    30-40 s per call); the image edit keeps its own 60 s budget."""
    s = _settings(llm_gateway="openrouter")
    assert s.llm_timeout_seconds == 40
    models = _ModelConfig.from_settings(s)
    for purpose in ("generation", "vision", "embedding"):
        assert credentials_for(models, purpose).timeout_seconds == 40.0
    for creds in (
        classify_credentials(s),
        column_map_credentials(s),
        size_chart_read_credentials(s),
    ):
        assert creds.timeout_seconds == 40.0
    assert image_edit_credentials(s).timeout_seconds == 60.0
    tuned = _settings(llm_gateway="openrouter", image_edit_timeout_seconds=90)
    assert image_edit_credentials(tuned).timeout_seconds == 90.0
    assert credentials_for(_ModelConfig.from_settings(tuned), "vision").timeout_seconds == 40.0


@pytest.mark.parametrize("field", ["llm_timeout_seconds", "image_edit_timeout_seconds"])
@pytest.mark.parametrize("value", [0, 121])
def test_timeouts_are_bounded(field: str, value: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _settings(**{field: value})
