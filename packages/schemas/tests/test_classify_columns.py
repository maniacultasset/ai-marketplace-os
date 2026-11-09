"""SI14 seam models: classify_product and map_columns payloads (aligned with the pipeline's
contract 1.1.0), and their Protocols."""

from __future__ import annotations

import base64
import typing
from typing import Any
from uuid import uuid4

import mkos_schemas.pipeline as pipeline_mod
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.classify import (
    MAX_CATEGORY_OPTIONS,
    MAX_CLASSIFY_IMAGES,
    CategoryCandidate,
    ClassifyRequest,
    ClassifyResult,
)
from mkos_schemas.columns import (
    MAX_LEFTOVER_HEADERS,
    ColumnMapRequest,
    ColumnMapResult,
)
from pydantic import ValidationError


def _image() -> dict[str, Any]:
    return {
        "image_id": str(uuid4()),
        "slot": "main",
        "width": 800,
        "height": 800,
        "data_b64": base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 12).decode(),
    }


def _classify(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "tenant_id": str(uuid4()),
        "product_id": str(uuid4()),
        "job_id": str(uuid4()),
        "request_id": "req-1",
        "text": {"title": "Cotton crew tee", "attributes": {"fabric": "cotton"}},
        "images": [],
        "categories": [{"category": "t_shirts"}, {"category": "shirts"}],
    }
    raw.update(changes)
    return raw


def _candidate(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "category": "t_shirts",
        "confidence": 0.9,
        "rationale": "A crew-neck tee.",
        "evidence_refs": ["text.title"],
    }
    raw.update(changes)
    return raw


def _result(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "status": "ok",
        "candidates": [_candidate()],
        "needs_confirmation": False,
        "calibration_version": "v1",
    }
    raw.update(changes)
    return raw


def _columns(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "tenant_id": str(uuid4()),
        "job_id": str(uuid4()),
        "request_id": "req-2",
        "headers": [{"header": "Colour Name"}],
        "dictionary": [{"key": "color_name", "label": "Colour"}],
    }
    raw.update(changes)
    return raw


def test_round_trips() -> None:
    six = [{**_image(), "image_id": str(uuid4())} for _ in range(MAX_CLASSIFY_IMAGES)]
    assert len(ClassifyRequest.model_validate(_classify(images=six)).images) == 6
    request = ClassifyRequest.model_validate(_classify())
    assert ClassifyRequest.model_validate_json(request.model_dump_json()) == request
    result = ClassifyResult.model_validate(_result())
    assert ClassifyResult.model_validate_json(result.model_dump_json()) == result
    columns = ColumnMapRequest.model_validate(_columns())
    assert ColumnMapRequest.model_validate_json(columns.model_dump_json()) == columns
    mapped = ColumnMapResult.model_validate(
        {
            "status": "ok",
            "mappings": {"Colour Name": {"key": "color_name", "confidence": 0.9}},
            "confidence_version": "columns-conf/v1",
        }
    )
    assert ColumnMapResult.model_validate_json(mapped.model_dump_json()) == mapped
    assert columns.headers[0].samples == []


@pytest.mark.parametrize(
    "raw",
    [
        _classify(images=[_image()] * (MAX_CLASSIFY_IMAGES + 1)),
        _classify(categories=[]),
        _classify(categories=[{"category": f"cat_{n}"} for n in range(MAX_CATEGORY_OPTIONS + 1)]),
        _classify(categories=[{"category": "t_shirts"}, {"category": "t_shirts"}]),
        _classify(text={"title": ""}),
    ],
)
def test_classify_request_refusals(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClassifyRequest.model_validate(raw)


@pytest.mark.parametrize(
    "raw",
    [
        _result(candidates=[_candidate()] * 4),
        _result(candidates=[_candidate(confidence=float("nan"))]),
        _result(candidates=[_candidate(confidence=float("inf"))]),
        _result(candidates=[_candidate(confidence=-0.1)]),
        _result(candidates=[_candidate(confidence=1.01)]),
        _result(candidates=[_candidate(rationale="x" * 301)]),
        _result(candidates=[_candidate(category="T Shirts")]),
        _result(status="maybe"),
        _result(calibration_version=""),
    ],
)
def test_classify_result_refusals(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClassifyResult.model_validate(raw)


@pytest.mark.parametrize("header", ["", "   ", "a\x00b", "line\nbreak", "x" * 201])
def test_header_refusals(header: str) -> None:
    with pytest.raises(ValidationError):
        ColumnMapRequest.model_validate(_columns(headers=[{"header": header}]))


def test_column_request_caps() -> None:
    with pytest.raises(ValidationError):
        ColumnMapRequest.model_validate(
            _columns(headers=[{"header": f"h{n}"} for n in range(MAX_LEFTOVER_HEADERS + 1)])
        )
    with pytest.raises(ValidationError):
        ColumnMapRequest.model_validate(_columns(dictionary=[]))


def test_unicode_header_accepted() -> None:
    assert ColumnMapRequest.model_validate(_columns(headers=[{"header": "रंग"}]))


def test_unknown_keys_dropped_on_the_wire() -> None:
    # WireModel: a newer peer's extra key is dropped, never re-emitted
    result = ClassifyResult.model_validate(_result(extra_field=1))
    assert "extra_field" not in result.model_dump()


def test_protocols_take_request_and_credentials() -> None:
    for proto, request, result in (
        (pipeline_mod.ClassifyProductFn, ClassifyRequest, ClassifyResult),
        (pipeline_mod.MapColumnsFn, ColumnMapRequest, ColumnMapResult),
    ):
        hints = typing.get_type_hints(proto.__call__)
        assert hints["request"] is request and hints["return"] is result


_slug = st.from_regex(r"[a-z0-9_]{2,20}", fullmatch=True)
_candidates = st.builds(
    lambda category, confidence, rationale: CategoryCandidate(
        category=category, confidence=confidence, rationale=rationale
    ),
    _slug,
    st.floats(min_value=0, max_value=1, allow_nan=False),
    st.text(max_size=300),
)


@given(st.lists(_candidates, max_size=3), st.booleans())
@settings(max_examples=60)
def test_any_valid_result_round_trips(candidates: list[CategoryCandidate], flag: bool) -> None:
    result = ClassifyResult(
        status="ok", candidates=candidates, needs_confirmation=flag, calibration_version="v1"
    )
    assert ClassifyResult.model_validate_json(result.model_dump_json()) == result
