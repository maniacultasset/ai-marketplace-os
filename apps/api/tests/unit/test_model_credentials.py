"""B2 (BE-01; ai-deps C18): `get_model_credentials` per purpose, built straight from `Settings`
with no database (the port reads only the `_ModelConfig` snapshot)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"


def _deps(app_env: str = "local", **over: Any) -> Any:
    from app.core.config import Settings
    from app.services.pipeline_deps import BackendPipelineDeps, _ModelConfig
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.enums import Marketplace
    from mkos_schemas.rules import RuleSet

    cfg = Settings(_env_file=None, app_env=app_env, database_url=LOCAL_URL, **over)
    ruleset = RuleSet(marketplace="amazon", category="kurta", loaded_at=datetime.now(UTC))
    return BackendPipelineDeps(
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        user_id=None,
        marketplace=Marketplace.AMAZON,
        rules=LoadedRules(ruleset=ruleset, template=None),
        product=None,
        image_reports={},
        embedder=None,
        models=_ModelConfig.from_settings(cfg),
        critical_keys=frozenset(),
        rejections={},
    )


async def _creds(deps: Any, purpose: str) -> tuple[str, str, str]:
    creds = await deps.get_model_credentials(purpose)
    return creds.provider, creds.model, creds.api_key.get_secret_value()


async def test_vision_defaults_to_openai_gpt_4o_mini() -> None:
    deps = _deps(openai_api_key="sk-openai")
    assert await _creds(deps, "vision") == ("openai", "gpt-4o-mini", "sk-openai")


async def test_vision_uses_its_own_model() -> None:
    deps = _deps(openai_api_key="sk-openai", llm_model_vision="gpt-4o", llm_model_default="m-gen")
    assert await _creds(deps, "vision") == ("openai", "gpt-4o", "sk-openai")
    assert await _creds(deps, "generation") == ("openai", "m-gen", "sk-openai")


async def test_vision_stays_openai_under_deepseek_generation() -> None:
    deps = _deps(llm_provider="deepseek", openai_api_key="sk-openai", deepseek_api_key="sk-deep")
    assert await _creds(deps, "generation") == ("deepseek", "gpt-4o-mini", "sk-deep")
    assert await _creds(deps, "vision") == ("openai", "gpt-4o-mini", "sk-openai")
    assert await _creds(deps, "embedding") == ("openai", "text-embedding-3-small", "sk-openai")


async def test_vision_without_openai_key_outside_local_is_unavailable() -> None:
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    deps = _deps("staging", llm_provider="deepseek", deepseek_api_key="sk-deep")
    assert (await _creds(deps, "generation"))[2] == "sk-deep"
    with pytest.raises(SeamPortError) as caught:
        await deps.get_model_credentials("vision")
    assert (caught.value.code, caught.value.field) == (ErrorCode.MODEL_UNAVAILABLE, "vision")


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_vision_local_placeholder_when_unset(key: str | None) -> None:
    deps = _deps(openai_api_key=key)
    assert (await _creds(deps, "vision"))[2] == "local_only_model_key_unset"


async def test_vision_timeout_and_api_base() -> None:
    creds = await _deps(openai_api_key="sk-openai", llm_timeout_seconds=42).get_model_credentials(
        "vision"
    )
    assert creds.api_base is None and creds.timeout_seconds == 42


OPENROUTER = "https://openrouter.ai/api/v1"


async def test_direct_gateway_sends_no_api_base() -> None:
    deps = _deps(openai_api_key="sk-openai", openrouter_api_key="sk-router")
    for purpose in ("generation", "vision", "embedding"):
        creds = await deps.get_model_credentials(purpose)
        assert creds.api_base is None, purpose
        assert creds.api_key.get_secret_value() == "sk-openai", purpose


async def test_openrouter_gateway_routes_every_purpose_with_one_key() -> None:
    """ADR-0024: generation keeps LLM_PROVIDER and LLM_MODEL_DEFAULT (an OpenRouter id); vision
    and embedding name their OpenAI model as `openai/<model>`; one key and the pinned base."""
    deps = _deps(
        llm_gateway="openrouter",
        llm_provider="deepseek",
        llm_model_default="deepseek/deepseek-chat",
        llm_model_vision="gpt-4o",
        openai_api_key="sk-openai",
        deepseek_api_key="sk-deep",
        openrouter_api_key="sk-router",
    )
    expected = {
        "generation": ("deepseek", "deepseek/deepseek-chat"),
        "vision": ("openai", "openai/gpt-4o"),
        "embedding": ("openai", "openai/text-embedding-3-small"),
    }
    for purpose, (provider, model) in expected.items():
        creds = await deps.get_model_credentials(purpose)
        assert (creds.provider, creds.model, creds.api_base) == (provider, model, OPENROUTER)
        assert creds.api_key.get_secret_value() == "sk-router", purpose


async def test_openrouter_without_its_key_outside_local_is_unavailable() -> None:
    """The direct keys never stand in for the gateway's key."""
    from app.services.pipeline_deps import SeamPortError
    from mkos_schemas.codes import ErrorCode

    deps = _deps(
        "staging", llm_gateway="openrouter", openai_api_key="sk-openai", deepseek_api_key="sk-d"
    )
    for purpose in ("generation", "vision", "embedding"):
        with pytest.raises(SeamPortError) as caught:
            await deps.get_model_credentials(purpose)
        assert (caught.value.code, caught.value.field) == (ErrorCode.MODEL_UNAVAILABLE, purpose)


async def test_openrouter_local_placeholder_key_keeps_the_base() -> None:
    deps = _deps(llm_gateway="openrouter")
    creds = await deps.get_model_credentials("generation")
    assert creds.api_base == OPENROUTER
    assert creds.api_key.get_secret_value() == "local_only_model_key_unset"


def test_model_config_repr_hides_the_router_key() -> None:
    deps = _deps(llm_gateway="openrouter", openrouter_api_key="sk-router-visible")
    assert "sk-router-visible" not in repr(deps._models)


def test_model_config_repr_hides_every_key() -> None:
    deps = _deps(
        llm_provider="deepseek", openai_api_key="sk-o-visible", deepseek_api_key="sk-d-vis"
    )
    shown = repr(deps._models)
    assert "sk-o-visible" not in shown and "sk-d-vis" not in shown
    assert "gpt-4o-mini" in shown  # the snapshot still names its models
