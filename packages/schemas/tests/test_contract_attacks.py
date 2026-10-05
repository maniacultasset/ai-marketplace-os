"""Tester attacks on the v1.0.0 contract that hold today (S2 round 1)."""

from __future__ import annotations

import importlib
import inspect
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.base import SEAM_SECRET_DENY_NAMES, StrictModel, WireModel
from mkos_schemas.export import MODELS
from mkos_schemas.jobs import BrandChunkDTO, GenerationContext
from mkos_schemas.paths import is_valid_field_path
from pydantic import BaseModel, ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MODEL_MODULES = (
    "agent",
    "auth",
    "errors",
    "images",
    "imports",
    "jobs",
    "listing",
    "publish",
    "rules",
    "validation",
)


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _schema_titles() -> set[str]:
    titles: set[str] = set()
    for model in MODELS:
        schema = model.model_json_schema(mode="serialization")
        titles.add(model.__name__)
        titles.update(schema.get("$defs", {}).keys())
    return titles


def test_every_public_model_is_reachable_from_an_exported_schema() -> None:
    # AC 2: a model that no exported schema reaches has no golden fixture guarding it.
    reachable = _schema_titles()
    missing: list[str] = []
    for name in MODEL_MODULES:
        module = importlib.import_module(f"mkos_schemas.{name}")
        for attr, obj in vars(module).items():
            if attr.startswith("_") or not inspect.isclass(obj):
                continue
            if not issubclass(obj, BaseModel) or obj in (BaseModel, StrictModel, WireModel):
                continue
            if obj.__module__ != module.__name__:
                continue
            if attr not in reachable:
                missing.append(f"{name}.{attr}")
    assert missing == []


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.__name__)
def test_unknown_key_is_dropped_or_rejected_per_base(model: type[BaseModel]) -> None:
    # AC 12 over every exported model, not only the base classes.
    raw = _fixture(model.__name__)
    raw["zz_unknown_key"] = {"nested": 1}
    if issubclass(model, StrictModel):
        with pytest.raises(ValidationError):
            model.model_validate(raw)
        return
    assert issubclass(model, WireModel)
    obj = model.model_validate(raw)
    assert "zz_unknown_key" not in obj.model_dump(by_alias=True, mode="json")
    assert "zz_unknown_key" not in obj.model_dump_json(by_alias=True)


@pytest.mark.parametrize(
    ("given", "wire"),
    [
        ("0", "0.000000"),
        ("1.5", "1.500000"),
        ("123456.123456", "123456.123456"),
        ("0.000001", "0.000001"),
        (2, "2.000000"),
    ],
)
def test_cost_usd_json_is_numeric_12_6_and_round_trips(given: object, wire: str) -> None:
    raw = _fixture("AgentRunRecord")
    raw["cost_usd"] = given
    record = AgentRunRecord.model_validate(raw)
    dumped = json.loads(record.model_dump_json())
    assert dumped["cost_usd"] == wire
    again = AgentRunRecord.model_validate_json(record.model_dump_json())
    assert again.cost_usd == Decimal(wire)


def test_cost_usd_rejects_more_than_12_digits() -> None:
    raw = _fixture("AgentRunRecord")
    raw["cost_usd"] = "1234567.000000"
    with pytest.raises(ValidationError):
        AgentRunRecord.model_validate(raw)


@pytest.mark.parametrize("name", sorted(SEAM_SECRET_DENY_NAMES))
def test_seam_walk_rejects_each_name_deep_in_product(name: str) -> None:
    raw = _fixture("GenerationContext")
    raw["product"] = {"a": {"b": [{"c": {name: "x"}}]}}
    with pytest.raises(ValidationError):
        GenerationContext.model_validate(raw)


@pytest.mark.parametrize("name", sorted(SEAM_SECRET_DENY_NAMES))
def test_seam_walk_rejects_each_name_in_embedded_json_text(name: str) -> None:
    raw = _fixture("GenerationContext")
    raw["brand_profile"] = {"notes": json.dumps({"outer": {name: "x"}})}
    with pytest.raises(ValidationError):
        GenerationContext.model_validate(raw)


def test_brand_chunk_text_with_secret_json_is_rejected() -> None:
    raw = _fixture("BrandChunkDTO")
    raw["text"] = json.dumps({"client_secret": "x"})
    with pytest.raises(ValidationError):
        BrandChunkDTO.model_validate(raw)


@pytest.mark.parametrize(
    "path",
    [
        "__class__",
        "content.__class__",
        "content.title.__class__",
        "model_config",
        "content.model_fields",
        "images[0].__dict__",
        "content.title\n",
    ],
)
def test_field_paths_reject_python_attribute_traversal(path: str) -> None:
    assert not is_valid_field_path(path)
