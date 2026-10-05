"""PipelineDeps protocol and re-exports."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from typing import cast

import mkos_schemas
import mkos_schemas.pipeline as pipeline_mod
import pytest
from mkos_schemas.jobs import GenerationContext
from mkos_schemas.pipeline import PipelineDeps, run_listing_pipeline

EXPECTED_PORTS = (
    "retrieve_brand_chunks",
    "validate_rules",
    "validate_images",
    "get_model_credentials",
    "emit_node_event",
)

DELETED_PORTS = (
    "load_rules",
    "load_category_template",
    "validate_schema",
    "record_agent_run",
)


def test_pipeline_deps_ports() -> None:
    found = {n for n in PipelineDeps.__dict__ if n in EXPECTED_PORTS}
    assert found == set(EXPECTED_PORTS)


def test_pipeline_deps_signatures() -> None:
    sig = inspect.signature(PipelineDeps.retrieve_brand_chunks)
    assert sig.parameters["k"].default == 8
    assert sig.parameters["types"].default is None


def test_pipeline_deps_ports_are_async() -> None:
    for name in EXPECTED_PORTS:
        assert inspect.iscoroutinefunction(getattr(PipelineDeps, name))


def test_deleted_ports_are_members_nowhere() -> None:
    for mod_info in pkgutil.walk_packages(mkos_schemas.__path__, mkos_schemas.__name__ + "."):
        mod = importlib.import_module(mod_info.name)
        for _, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod_info.name:
                continue
            for port in DELETED_PORTS:
                assert port not in cls.__dict__, f"{port} on {cls.__qualname__}"


def test_pipeline_reexports() -> None:
    expected_all = {
        "PipelineDeps",
        "run_listing_pipeline",
        "GenerationContext",
        "GenerationResult",
        "AgentBlockStatus",
        "AgentRunRecord",
        "BrandChunkDTO",
        "CategoryTemplate",
        "ModelCredentials",
        "Rule",
        "RuleSet",
    }
    assert len(pipeline_mod.__all__) == len(expected_all)
    assert set(pipeline_mod.__all__) == expected_all


def test_pipeline_defines_only_protocol_and_signature() -> None:
    owned = {
        name
        for name, obj in pipeline_mod.__dict__.items()
        if inspect.isclass(obj) or inspect.isfunction(obj)
        if getattr(obj, "__module__", None) == pipeline_mod.__name__
    }
    assert owned == {"PipelineDeps", "run_listing_pipeline"}


def test_run_listing_pipeline_signature() -> None:
    assert inspect.iscoroutinefunction(run_listing_pipeline)

    class _Deps:
        async def retrieve_brand_chunks(self, *a: object, **k: object) -> None: ...
        async def validate_rules(self, *a: object, **k: object) -> None: ...
        async def validate_images(self, *a: object, **k: object) -> None: ...
        async def get_model_credentials(self, *a: object, **k: object) -> None: ...
        async def emit_node_event(self, *a: object, **k: object) -> None: ...

    ctx = GenerationContext.model_construct(**{})
    with pytest.raises(NotImplementedError):
        asyncio.run(run_listing_pipeline(ctx, cast(PipelineDeps, _Deps())))


def test_agent_state_and_telemetry_absent() -> None:
    for mod_info in pkgutil.walk_packages(mkos_schemas.__path__, mkos_schemas.__name__ + "."):
        mod = importlib.import_module(mod_info.name)
        assert not hasattr(mod, "AgentState")
        assert "telemetry" not in mod_info.name
