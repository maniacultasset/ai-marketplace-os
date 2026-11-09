"""SI12 seam models: SizeChartReadRequest, SizeChartReadResult, ContextImage.role, ReadSizeChart."""

from __future__ import annotations

import asyncio
import copy
import json
import typing
from pathlib import Path
from typing import Any

import mkos_schemas.pipeline as pipeline_mod
import pytest
from mkos_schemas.enums import ImageRole
from mkos_schemas.jobs import ContextImage, ModelCredentials
from mkos_schemas.pipeline import ReadSizeChart, read_size_chart
from mkos_schemas.size_chart_read import (
    MAX_SIZE_CHART_MEASURE,
    MAX_SIZE_CHART_READ_ROWS,
    MAX_SIZE_CHART_SIZE_CHARS,
    MAX_SIZE_CHART_UNIT_LINE_CHARS,
    SIZE_CHART_MEASURE_DECIMALS,
    SIZE_CHART_MEASURE_KEYS,
    SizeChartReadChart,
    SizeChartReadRequest,
    SizeChartReadResult,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return raw


def _request(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_fixture("SizeChartReadRequest"))
    raw.update(changes)
    return raw


def _result(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_fixture("SizeChartReadResult"))
    raw.update(changes)
    return raw


def test_fixtures_round_trip() -> None:
    for name, model in (
        ("SizeChartReadRequest", SizeChartReadRequest),
        ("SizeChartReadResult", SizeChartReadResult),
    ):
        raw = _fixture(name)
        assert model.model_validate(raw).model_dump(mode="json") == raw


def test_request_image_is_the_size_chart() -> None:
    req = SizeChartReadRequest.model_validate(_request())
    assert req.image.slot == "size_chart"
    assert req.attempt == 1


@pytest.mark.parametrize("attempt", [0, 4, -1])
def test_attempt_bounds(attempt: int) -> None:
    with pytest.raises(ValidationError):
        SizeChartReadRequest.model_validate(_request(attempt=attempt))


@pytest.mark.parametrize("category", ["", "Bad-Slug", "a", "x" * 65])
def test_category_is_a_slug(category: str) -> None:
    with pytest.raises(ValidationError):
        SizeChartReadRequest.model_validate(_request(category=category))


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1])
def test_confidence_bounds(confidence: float) -> None:
    with pytest.raises(ValidationError):
        SizeChartReadResult.model_validate(_result(confidence=confidence))


def test_confidence_may_be_null() -> None:
    assert SizeChartReadResult.model_validate(_result(confidence=None)).confidence is None


def test_unknown_result_key_is_dropped() -> None:
    parsed = SizeChartReadResult.model_validate(_result(newer_field={"x": 1}))
    assert "newer_field" not in parsed.model_dump()


def test_rows_cap() -> None:
    chart = copy.deepcopy(_fixture("SizeChartReadResult")["chart"])
    row = chart["rows"][0]
    chart["rows"] = [dict(row) for _ in range(MAX_SIZE_CHART_READ_ROWS + 1)]
    with pytest.raises(ValidationError):
        SizeChartReadResult.model_validate(_result(chart=chart))
    chart["rows"] = chart["rows"][:MAX_SIZE_CHART_READ_ROWS]
    assert SizeChartReadResult.model_validate(_result(chart=chart)).chart is not None


def test_empty_rows_refused() -> None:
    with pytest.raises(ValidationError):
        SizeChartReadChart.model_validate({"unit": "cm", "basis": "garment", "rows": []})


@pytest.mark.parametrize(
    "change", [{"unit": "mm"}, {"basis": "model"}, {"rows": [{"size": ["S"]}]}]
)
def test_chart_shape(change: dict[str, Any]) -> None:
    chart = {"unit": "cm", "basis": "garment", "rows": [{"size": "S", "chest": 96}]} | change
    with pytest.raises(ValidationError):
        SizeChartReadChart.model_validate(chart)


def test_rows_stay_loose_for_the_backend_to_normalise() -> None:
    # Unknown row keys and odd numbers reach the backend, which normalises or refuses them.
    chart = SizeChartReadChart.model_validate(
        {"unit": "in", "basis": "body", "rows": [{"size": "S", "shoulder_x": 900}]}
    )
    assert chart.rows == [{"size": "S", "shoulder_x": 900}]


def test_agent_runs_cap() -> None:
    record = _fixture("SizeChartReadResult")["agent_runs"][0]
    with pytest.raises(ValidationError):
        SizeChartReadResult.model_validate(_result(agent_runs=[record, record, record]))


def test_unreadable_result_without_chart() -> None:
    parsed = SizeChartReadResult.model_validate(_result(chart=None, unreadable=True))
    assert parsed.chart is None and parsed.unreadable


def test_context_image_role_is_optional() -> None:
    image = copy.deepcopy(_fixture("SizeChartReadRequest")["image"])
    image.pop("role", None)
    assert ContextImage.model_validate(image).role is None
    image["slot"] = "secondary"
    image["role"] = "back"
    assert ContextImage.model_validate(image).role == "back"


@pytest.mark.parametrize("role", ["front", "main", "", "BACK"])
def test_context_image_role_is_closed(role: str) -> None:
    image = copy.deepcopy(_fixture("SizeChartReadRequest")["image"])
    image["role"] = role
    with pytest.raises(ValidationError):
        ContextImage.model_validate(image)


def test_image_role_members() -> None:
    assert set(typing.get_args(ImageRole)) == {"back", "close_up"}


def test_request_repr_hides_the_bytes() -> None:
    req = SizeChartReadRequest.model_validate(_request())
    assert req.image.data_b64 not in repr(req)
    bad = _request()
    bad["image"]["data_b64"] = "not-base64!!" + req.image.data_b64
    with pytest.raises(ValidationError) as caught:
        SizeChartReadRequest.model_validate(bad)
    assert req.image.data_b64 not in str(caught.value)


def test_read_size_chart_contract() -> None:
    assert "read_size_chart" in pipeline_mod.__all__
    assert "ReadSizeChart" in pipeline_mod.__all__
    hints = typing.get_type_hints(read_size_chart)
    assert hints == {
        "request": SizeChartReadRequest,
        "credentials": ModelCredentials,
        "return": SizeChartReadResult,
    }
    call_hints = typing.get_type_hints(ReadSizeChart.__call__)
    assert call_hints["request"] is SizeChartReadRequest
    assert call_hints["return"] is SizeChartReadResult
    req = SizeChartReadRequest.model_validate(_request())
    creds = ModelCredentials.model_validate(_fixture("ModelCredentials") | {"api_key": "k"})
    with pytest.raises(NotImplementedError):
        asyncio.run(read_size_chart(req, creds))


@pytest.mark.parametrize("slot", ["main", "lifestyle", "size_chart", "packaging_label"])
def test_context_image_role_only_on_secondary(slot: str) -> None:
    image = copy.deepcopy(_fixture("SizeChartReadRequest")["image"])
    image["slot"] = slot
    image["role"] = "back"
    with pytest.raises(ValidationError):
        ContextImage.model_validate(image)


def test_unit_may_be_unknown() -> None:
    # a chart that prints no unit, or one the pipeline cannot back, keeps its rows
    chart = SizeChartReadChart.model_validate(
        {"unit": "unknown", "basis": "garment", "rows": [{"size": "S", "chest": 96}]}
    )
    assert chart.unit == "unknown" and chart.unit_source_line is None


def test_unit_source_line_bounds() -> None:
    base = {"unit": "cm", "basis": "garment", "rows": [{"size": "S", "chest": 96}]}
    line = "x" * MAX_SIZE_CHART_UNIT_LINE_CHARS
    assert SizeChartReadChart.model_validate(base | {"unit_source_line": line}).unit_source_line
    for bad in ("", line + "x"):
        with pytest.raises(ValidationError):
            SizeChartReadChart.model_validate(base | {"unit_source_line": bad})


def test_bounds_constants() -> None:
    assert SIZE_CHART_MEASURE_KEYS == (
        "chest",
        "waist",
        "hip",
        "length",
        "shoulder",
        "sleeve",
        "inseam",
    )
    assert (MAX_SIZE_CHART_SIZE_CHARS, MAX_SIZE_CHART_MEASURE, SIZE_CHART_MEASURE_DECIMALS) == (
        20,
        500,
        1,
    )
    assert MAX_SIZE_CHART_UNIT_LINE_CHARS == 160
