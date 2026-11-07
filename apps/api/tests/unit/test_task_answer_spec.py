"""SI3 round 1 F1: `tasks_answer` validates a fact answer against the task's AnswerSpec
(DATABASE.md:1411, :1422), whichever lane opened the task. The HTTP path is
tests/integration/test_tasks_answer_api.py."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import InputError
from app.services.tasks import MAX_PIECES, _answer_value

ENUM: dict[str, Any] = {"kind": "enum", "allowed_values": ["red", "blue"]}


def _refused(key: str, value: object, spec: object) -> None:
    with pytest.raises(InputError) as caught:
        _answer_value(key, value, spec)
    assert caught.value.field == "value"


@pytest.mark.parametrize("value", ["banana", "maroon", 7, None, ["red"], ""])
def test_enum_refuses_a_value_outside_allowed_values(value: object) -> None:
    _refused("color", value, ENUM)


def test_enum_stores_the_canonical_form() -> None:
    assert _answer_value("color", " RED ", ENUM) == "red"
    assert _answer_value(
        "color", "Navy-Blue", {"kind": "enum", "allowed_values": ["navy blue"]}
    ) == ("navy blue")


def test_enum_outside_the_photo_vocabulary_matches_folded_text() -> None:
    spec = {"kind": "enum", "allowed_values": ["Bandhani", "Ikat"]}
    assert _answer_value("print_style", "ikat", spec) == "Ikat"
    _refused("print_style", "batik", spec)
    # a vocabulary key whose question lists a value the photo vocabulary does not know
    assert _answer_value(
        "color", "onion-pink", {"kind": "enum", "allowed_values": ["Onion Pink"]}
    ) == ("Onion Pink")


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "enum"},
        {"kind": "enum", "allowed_values": []},
        {"kind": "enum", "allowed_values": "red"},
    ],
)
def test_enum_without_allowed_values_accepts_nothing(spec: dict[str, Any]) -> None:
    _refused("color", "red", spec)


def test_multi_enum_checks_every_element() -> None:
    spec = {"kind": "multi_enum", "allowed_values": ["red", "blue"]}
    assert _answer_value("color", ["Red", "blue"], spec) == ["red", "blue"]
    for bad in (["red", "banana"], "red", [], [7]):
        _refused("color", bad, spec)


@pytest.mark.parametrize("value", [0, 4, True, 2.0, "3"])
def test_number_is_an_int_within_min_and_max(value: object) -> None:
    _refused("lining_count", value, {"kind": "number", "min": 1, "max": 3})


def test_number_in_range_is_kept() -> None:
    assert _answer_value("lining_count", 3, {"kind": "number", "min": 1, "max": 3}) == 3


@pytest.mark.parametrize(
    "spec", [{"kind": "number", "min": 1, "max": MAX_PIECES}, {}, None, {"kind": "text"}]
)
def test_pieces_is_always_a_count(spec: object) -> None:
    assert _answer_value("number_of_pieces", MAX_PIECES, spec) == MAX_PIECES
    for bad in (0, MAX_PIECES + 1, True, "3"):
        _refused("number_of_pieces", bad, spec)
