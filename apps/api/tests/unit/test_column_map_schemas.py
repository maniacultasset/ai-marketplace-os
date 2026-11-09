"""SI14 T4: the column-map request, answer and confirm bodies."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.v1.schemas.column_maps import ColumnMapConfirm, ColumnMapCreate
from pydantic import ValidationError


@pytest.mark.parametrize(
    "body",
    [
        {"headers": []},
        {"headers": [f"h{n}" for n in range(201)]},
        {"headers": ["a\x00b"]},
        {"headers": ["line\nbreak"]},
        {"headers": ["tab\there"]},
        {"headers": ["   "]},
        {"headers": ["x" * 121]},
        {"headers": ["colour"], "samples": {"colour": ["a", "b", "c", "d"]}},
        {"headers": ["colour"], "samples": {"colour": ["x" * 65]}},
        {"headers": ["colour"], "samples": {"colour": ["a\x01"]}},
        {"headers": ["colour"], "samples": {"size": ["m"]}},
        {"headers": ["colour"], "extra": 1},
    ],
)
def test_create_refusals(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ColumnMapCreate.model_validate(body)


def test_create_accepts_unicode_and_samples() -> None:
    body = ColumnMapCreate.model_validate(
        {"headers": ["रंग", "item sku"], "samples": {"रंग": ["लाल"]}}
    )
    assert body.headers == ["रंग", "item sku"]


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        [{"position": 0, "key": "color_name"}],
        [{"position": 201, "key": "color_name"}],
        [{"position": 1, "key": "a"}, {"position": 1, "key": "b"}],
        [{"position": 1, "key": "x" * 65}],
    ],
)
def test_confirm_refusals(mappings: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        ColumnMapConfirm.model_validate({"mappings": mappings})


def test_confirm_takes_a_null_key() -> None:
    body = ColumnMapConfirm.model_validate({"mappings": [{"position": 3, "key": None}]})
    assert body.mappings[0].key is None
