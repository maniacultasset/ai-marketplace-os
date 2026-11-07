"""SI4 A11 (AC9; D14; spec L1029-1037 "one clear question"): templated question text and the
expected answer. No template formats a value."""

from __future__ import annotations

from typing import get_args

import pytest
from app.db.platform_loads import DictionaryEntry
from app.services.auto_fix.policy import MISSING_FACT, POLICY, Route
from app.services.auto_fix.text import (
    ANSWER_KINDS,
    QUESTION_MAX,
    QUESTION_MIN,
    affected,
    expected_answer,
    image_question,
    question_for,
)
from mkos_schemas.codes import ErrorCode

ASKED = [c for c, r in POLICY.items() if r.route is Route.ASK and r.task_kind == MISSING_FACT]
ASKED.append(ErrorCode.VALUE_NOT_ALLOWED)  # the remap's fallback question (CP-D15)
VALUE_TYPES = get_args(DictionaryEntry.model_fields["value_type"].annotation)
SENTINEL = "SENTINEL-VALUE-7f3a"


def _fits(text: str) -> bool:
    return QUESTION_MIN <= len(text) <= QUESTION_MAX


@pytest.mark.parametrize("code", ASKED)
def test_every_asked_code_has_a_template(code: ErrorCode) -> None:
    question, reason = question_for(code, "fiber_composition", "Fabric composition")
    assert _fits(question) and _fits(reason)
    assert "Fabric composition".casefold() in question.casefold()


@pytest.mark.parametrize("code", ASKED)
@pytest.mark.parametrize("label", ["x" * 64, "कपड़े की संरचना", "Fabric composition 😀", "", "   "])
def test_lengths_hold_for_long_unicode_and_blank_labels(code: ErrorCode, label: str) -> None:
    key = "k" * 64
    question, reason = question_for(code, key, label, value_type="composition")
    assert _fits(question) and _fits(reason)
    if not label.strip():
        assert "k k" not in question and key.replace("_", " ") in question


@pytest.mark.parametrize("value_type", VALUE_TYPES)
def test_every_dictionary_value_type_maps_to_an_answer_kind(value_type: str) -> None:
    spec = expected_answer(value_type, vocabulary=["A", "B"], unit="cm")
    assert spec["kind"] in ANSWER_KINDS


def test_answer_specs() -> None:
    assert expected_answer("enum", vocabulary=["Cotton", "Linen"], unit=None) == {
        "kind": "enum",
        "allowed_values": ["Cotton", "Linen"],
    }
    assert expected_answer("enum_list", vocabulary=["A"], unit=None)["kind"] == "multi_enum"
    assert expected_answer("measure", vocabulary=None, unit="cm") == {
        "kind": "number",
        "unit": "cm",
    }
    assert expected_answer("integer", vocabulary=None, unit=None) == {"kind": "number"}
    assert expected_answer("money", vocabulary=None, unit=None) == {"kind": "money_inr"}
    assert expected_answer("composition", vocabulary=None, unit=None) == {
        "kind": "percent_composition"
    }
    assert expected_answer("boolean", vocabulary=None, unit=None) == {"kind": "boolean"}
    assert expected_answer("text", vocabulary=None, unit=None) == {
        "kind": "text",
        "max_length": 256,
    }
    assert expected_answer("enum", vocabulary=None, unit=None) == {
        "kind": "text",
        "max_length": 256,
    }


def test_image_question() -> None:
    question, reason = image_question("main", ["background", "text_overlay"])
    assert _fits(question) and _fits(reason)
    assert "main" in question
    long_q, long_r = image_question("main", ["c" * 60] * 30)
    assert _fits(long_q) and _fits(long_r)


def test_affected_clause() -> None:
    clause = affected([("KURTA-001", "amazon"), ("KURTA-001", "flipkart")])
    assert "KURTA-001" in clause and "Amazon" in clause and "Flipkart" in clause
    assert affected([]) == ""
    many = affected([(f"SKU-{i}", "amazon") for i in range(80)])
    assert len(many) <= 300


@pytest.mark.parametrize("code", ASKED)
def test_no_template_formats_a_value(code: ErrorCode) -> None:
    question, reason = question_for(code, "fit", "Fit", value_type="enum")
    assert SENTINEL not in question + reason
    # the signature takes no value at all: a value cannot reach the text
    with pytest.raises(TypeError):
        question_for(code, "fit", "Fit", value=SENTINEL)  # type: ignore[call-arg]
