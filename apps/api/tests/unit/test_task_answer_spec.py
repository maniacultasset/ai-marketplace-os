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


@pytest.mark.parametrize("value", [0, 4, 0.5, 3.5, True, False, "3", None, [2]])
def test_number_is_within_min_and_max(value: object) -> None:
    _refused("lining_count", value, {"kind": "number", "min": 1, "max": 3})


@pytest.mark.parametrize("value", [1, 3, 2.0, 2.5])
def test_number_in_range_is_kept(value: float) -> None:
    assert _answer_value("lining_count", value, {"kind": "number", "min": 1, "max": 3}) == value


def test_number_bounds_may_be_floats() -> None:
    spec = {"kind": "number", "min": 40.5, "max": 300.25}
    assert _answer_value("gsm", 180.5, spec) == 180.5
    for bad in (40.4, 300.26, 40, 301):
        _refused("gsm", bad, spec)
    assert _answer_value("gsm", 9999, {"kind": "number"}) == 9999


@pytest.mark.parametrize(
    "spec", [{"kind": "number", "min": 1, "max": MAX_PIECES}, {}, None, {"kind": "text"}]
)
def test_pieces_is_always_a_count(spec: object) -> None:
    assert _answer_value("number_of_pieces", MAX_PIECES, spec) == MAX_PIECES
    for bad in (0, MAX_PIECES + 1, True, "3"):
        _refused("number_of_pieces", bad, spec)


TEXT_256: dict[str, Any] = {"kind": "text", "max_length": 256}


def test_text_is_a_string_within_max_length() -> None:
    assert _answer_value("fit", "a" * 256, TEXT_256) == "a" * 256
    for bad in ("a" * 257, True, 7, 1.5, ["a"]):
        _refused("fit", bad, TEXT_256)
    # no max_length: any string; the HTTP schema bounds it (AnswerScalar)
    assert _answer_value("fit", "a" * 1500, {"kind": "text"}) == "a" * 1500


def test_boolean_takes_only_a_bool() -> None:
    assert _answer_value("lined", True, {"kind": "boolean"}) is True
    assert _answer_value("lined", False, {"kind": "boolean"}) is False
    for bad in ("banana", "true", 1, 0, None):
        _refused("lined", bad, {"kind": "boolean"})


def test_money_inr_is_a_positive_decimal_string() -> None:
    assert _answer_value("mrp", "1299.00", {"kind": "money_inr"}) == "1299.00"
    for bad in ("0", "-5", "12.345", "abc", 1299, 1299.0, True):
        _refused("mrp", bad, {"kind": "money_inr"})


def test_percent_composition_takes_a_string() -> None:
    # the composition value_type parses it in build_attributes
    spec = {"kind": "percent_composition"}
    assert _answer_value("fabric", "95% cotton / 5% elastane", spec) == "95% cotton / 5% elastane"
    for bad in (95, True, 9.5):
        _refused("fabric", bad, spec)


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "file"},
        {"kind": "acknowledge"},
        {"kind": "reconnect"},
        {"kind": "colour_wheel"},
        {"kind": None},
        {"kind": 7},
        {"allowed_values": ["red"]},
        {},
        None,
        [],
        "text",
    ],
)
def test_a_kind_that_takes_no_value_or_is_unknown_refuses_a_value(spec: object) -> None:
    for value in ("red", True, 3):
        _refused("color", value, spec)
