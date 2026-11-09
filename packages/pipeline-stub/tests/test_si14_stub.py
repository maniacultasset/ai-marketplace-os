"""SI14 T5: the stub `classify_product` and `map_columns` are deterministic and record the request."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import mkos_pipeline
import pytest
from mkos_pipeline import classify as stub_classify
from mkos_pipeline import classify_product, map_columns
from mkos_pipeline import columns as stub_columns
from mkos_schemas.classify import ClassifyRequest, ClassifyResult
from mkos_schemas.columns import ColumnMapRequest, ColumnMapResult
from mkos_schemas.jobs import ModelCredentials

SCHEMA_FIXTURES = Path(__file__).resolve().parents[2] / "schemas" / "fixtures"


def _load(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((SCHEMA_FIXTURES / f"{name}.json").read_text("utf-8"))
    return raw


def _creds() -> ModelCredentials:
    return ModelCredentials.model_validate(_load("ModelCredentials") | {"api_key": "k"})


def _classify(title: str, **changes: Any) -> ClassifyRequest:
    raw = _load("ClassifyRequest")
    raw["text"] = {**raw["text"], "title": title}
    return ClassifyRequest.model_validate(raw | changes)


def _columns(*headers: str, **changes: Any) -> ColumnMapRequest:
    raw = _load("ColumnMapRequest") | {"headers": [{"header": h} for h in headers]}
    return ColumnMapRequest.model_validate(raw | changes)


def test_exports() -> None:
    assert {"classify_product", "map_columns"} <= set(mkos_pipeline.__all__)


def test_classify_offers_the_first_category() -> None:
    request = _classify("Kurta set")
    result = asyncio.run(classify_product(request, _creds()))
    assert isinstance(result, ClassifyResult)
    assert [c.category for c in result.candidates] == [request.categories[0].category]
    assert result.candidates[0].confidence == stub_classify.STUB_CONFIDENCE
    assert not result.needs_confirmation
    (record,) = result.agent_runs
    assert (record.agent_name, record.trigger) == ("attribute", "vision")
    assert (record.prompt_version or "").startswith("classify/")
    assert stub_classify.LAST_REQUEST is request
    again = asyncio.run(classify_product(request, _creds()))
    assert again == result  # deterministic


def test_classify_without_photos_is_generate() -> None:
    result = asyncio.run(classify_product(_classify("Kurta", images=[]), _creds()))
    assert result.agent_runs[0].trigger == "generate"


@pytest.mark.parametrize(
    ("marker", "check"),
    [
        ("[lowconf]", lambda r: r.candidates[0].confidence == 0.5 and r.needs_confirmation),
        ("[none]", lambda r: r.candidates == [] and r.needs_confirmation),
        ("[outside]", lambda r: r.candidates[0].category == stub_classify.OUTSIDE_CATEGORY),
        ("[bidi]", lambda r: "\x00" in r.candidates[0].rationale),
        ("[badrecord]", lambda r: r.agent_runs[0].agent_name == "content"),
    ],
)
def test_classify_markers(marker: str, check: Any) -> None:
    assert check(asyncio.run(classify_product(_classify(f"Tee {marker}"), _creds())))


def test_classify_boom_raises() -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(classify_product(_classify("Tee [boom]"), _creds()))


def test_map_columns_by_substring() -> None:
    request = _columns("Colour Name", "Wash care", "Shade lowconf color_name", "Mystery")
    result = asyncio.run(map_columns(request, _creds()))
    assert isinstance(result, ColumnMapResult)
    assert result.mappings["Colour Name"].key is None  # "colour_name" holds no dictionary key
    assert result.mappings["Shade lowconf color_name"].key == "color_name"
    assert result.mappings["Shade lowconf color_name"].confidence == 0.79
    assert result.mappings["Mystery"].key is None
    (record,) = result.agent_runs
    assert (record.agent_name, record.trigger, record.product_id) == ("attribute", "generate", None)
    assert stub_columns.LAST_REQUEST is request
    assert all(column.samples == [] for column in request.headers)


def test_map_columns_skips_taken_keys_and_outside() -> None:
    request = _columns("color_name", "outside", taken_keys=["color_name"])
    result = asyncio.run(map_columns(request, _creds()))
    assert result.mappings["color_name"].key is None
    assert result.mappings["outside"].key == stub_columns.OUTSIDE_KEY


def test_map_columns_boom_raises() -> None:
    with pytest.raises(RuntimeError):
        asyncio.run(map_columns(_columns("boom"), _creds()))


def test_ids_are_deterministic_per_request() -> None:
    a = asyncio.run(map_columns(_columns("x", request_id="r-1"), _creds()))
    b = asyncio.run(map_columns(_columns("x", request_id="r-1"), _creds()))
    c = asyncio.run(map_columns(_columns("x", request_id=str(uuid4())), _creds()))
    assert a.agent_runs[0].id == b.agent_runs[0].id != c.agent_runs[0].id
