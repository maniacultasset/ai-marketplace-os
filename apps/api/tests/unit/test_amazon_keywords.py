"""DW2 T5: Amazon's JSON Schema vocabulary keywords in `schema_check` (maxUtf8ByteLength,
minUtf8ByteLength, maxUniqueItems with its sibling `selectors`)."""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.amazon.schema_check import KEYWORDS, compile_schema
from hypothesis import given, settings
from hypothesis import strategies as st


def _valid(schema: dict[str, Any], instance: Any) -> bool:
    compiled = compile_schema(schema)
    return bool(compiled.validator.is_valid(instance))  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("value", "ok"),
    [("abcdefghij", True), ("कुर्ता", False), ("é" * 5, True), ("é" * 6, False), ("", True)],
)
def test_max_utf8_byte_length(value: str, ok: bool) -> None:
    assert _valid({"maxUtf8ByteLength": 10}, value) is ok


@given(text=st.text(max_size=200), limit=st.integers(min_value=0, max_value=500))
@settings(max_examples=200, deadline=None)
def test_max_utf8_byte_length_property(text: str, limit: int) -> None:
    keyword = KEYWORDS["maxUtf8ByteLength"]({}, limit, [])
    failed = False
    try:
        keyword.validate(text)
    except ValueError:
        failed = True
    assert failed is (len(text.encode("utf-8")) > limit)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("abcdefghij", True),
        ("abc", False),
        ("कुर्ता", True),
        ("é" * 2, False),
        (12, True),
        (None, True),
    ],
)
def test_min_utf8_byte_length(value: Any, ok: bool) -> None:
    assert _valid({"minUtf8ByteLength": 5}, value) is ok


def test_max_utf8_ignores_non_strings() -> None:
    assert _valid({"maxUtf8ByteLength": 1}, 12345)
    assert _valid({"maxUtf8ByteLength": 1}, ["long string"])


SELECTED = {"maxUniqueItems": 2, "selectors": ["marketplace_id", "language_tag"]}


def test_max_unique_items_with_selectors() -> None:
    a = {"value": "x", "marketplace_id": "A", "language_tag": "en_IN"}
    b = {"value": "y", "marketplace_id": "A", "language_tag": "hi_IN"}
    c = {"value": "z", "marketplace_id": "B", "language_tag": "en_IN"}
    same_as_a = {"value": "other", "marketplace_id": "A", "language_tag": "en_IN"}
    assert _valid(SELECTED, [a, b, same_as_a])
    assert not _valid(SELECTED, [a, b, c])
    # missing selector keys count as None
    assert _valid(SELECTED, [{"value": 1}, {"value": 2}, a])
    assert not _valid(SELECTED, [{"value": 1}, a, b])


def test_max_unique_items_without_selectors_is_whole_item() -> None:
    schema = {"maxUniqueItems": 1}
    assert _valid(schema, [{"v": 1}, {"v": 1}])
    assert not _valid(schema, [{"v": 1}, {"v": 2}])


def test_max_unique_items_ignores_non_arrays() -> None:
    assert _valid({"maxUniqueItems": 0}, {"v": 1})
    assert _valid({"maxUniqueItems": 0}, "text")


@pytest.mark.parametrize("bad", [-1, "3", True, 1.5, None])
def test_bad_keyword_limits_fail_to_compile(bad: Any) -> None:
    from app.adapters.amazon.schema_check import _CompileFailed

    assert isinstance(compile_schema({"maxUtf8ByteLength": bad}), _CompileFailed)
