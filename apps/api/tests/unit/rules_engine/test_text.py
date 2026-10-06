"""S14a T2/T3: normalisation, measuring, the tokenizer, slots and value access (PLAN D3)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pytest
from app.services.rules_engine import text as text_mod
from app.services.rules_engine.text import (
    UnsafeRuleRegexError,
    condition_holds,
    find_terms,
    is_present,
    iter_slots,
    lookup,
    measure,
    normalise,
)
from mkos_schemas.paths import FIELD_PATH_PATTERNS, RULE_FIELD_EXTRAS
from mkos_schemas.rules import Condition
from tests.rules_engine_builders import listing

FULLWIDTH_BEST = "".join(chr(0xFEE0 + ord(c)) for c in "BEST")
NBSP = chr(0xA0)
IDEOGRAPHIC_SPACE = chr(0x3000)


def test_normalise_nfkc_fullwidth() -> None:
    assert normalise(FULLWIDTH_BEST, case_sensitive=False) == "best"


def test_normalise_case_sensitive_keeps_case() -> None:
    assert normalise("Best  Deal", case_sensitive=True) == "Best Deal"


def test_normalise_collapses_tabs_newlines_nbsp() -> None:
    assert normalise(
        " free\t\n" + NBSP + " shipping" + IDEOGRAPHIC_SPACE + "now ", case_sensitive=False
    ) == ("free shipping now")


def test_normalise_empty_and_blank() -> None:
    assert normalise("", case_sensitive=False) == ""
    assert normalise(" \t\n ", case_sensitive=False) == ""


def test_measure_chars_counts_code_points() -> None:
    assert measure("😀", "chars") == 1
    nfc = unicodedata.normalize("NFC", "é")
    nfd = unicodedata.normalize("NFD", "é")
    assert len(nfd) == 2
    assert measure(nfc, "chars") == measure(nfd, "chars") == 1


def test_measure_bytes_utf8() -> None:
    assert measure("कुर्ता", "bytes") == len("कुर्ता".encode())
    assert measure("abc", "bytes") == 3


def test_measure_words() -> None:
    assert measure("  cotton   kurta\tset ", "words") == 3


def test_measure_list_joined_by_one_space() -> None:
    assert measure(["ab", "cd"], "chars") == 5
    assert measure(["ab", "cd"], "words") == 2


def test_measure_empty_list_is_zero() -> None:
    assert measure([], "chars") == 0
    assert measure([], "words") == 0


# ---- T3: tokenizer -------------------------------------------------------------------------


def _word(text: str, terms: list[str]) -> tuple[str, ...]:
    return find_terms(text, terms, match="word", case_sensitive=False)


def test_word_match_hash_one() -> None:
    assert _word("Rank #1 kurta", ["#1"]) == ("#1",)


def test_word_no_match_inside_word() -> None:
    assert _word("secure fit", ["cure"]) == ()


def test_word_match_at_text_edges() -> None:
    assert _word("best", ["best"]) == ("best",)
    assert _word("best kurta", ["best"]) == ("best",)
    assert _word("kurta best", ["best"]) == ("best",)
    assert _word("kurta, best!", ["best"]) == ("best",)


def test_word_devanagari_mark_is_word_char() -> None:
    # "कुर्ता" carries combining marks (category M): a term ending before a mark is inside a word.
    assert _word("कुर्ता", ["क"]) == ()
    assert _word("क कुर्ता", ["क"]) == ("क",)


def test_word_prefix_overlap() -> None:
    assert _word("#1 sellers", ["#1 seller", "#1"]) == ("#1",)


def test_word_later_occurrence_found() -> None:
    assert _word("secure cure", ["cure"]) == ("cure",)


def test_substring_matches_inside_word() -> None:
    assert find_terms("secure", ["cure"], match="substring", case_sensitive=False) == ("cure",)
    assert find_terms("secure", ["xyz"], match="substring", case_sensitive=False) == ()


def test_case_sensitive_true_pass_fail() -> None:
    assert find_terms("Best kurta", ["best"], match="word", case_sensitive=True) == ()
    assert find_terms("Best kurta", ["Best"], match="word", case_sensitive=True) == ("Best",)


def test_regex_term_ignore_case() -> None:
    assert find_terms("BEST deal", [r"best"], match="regex", case_sensitive=False) == ("best",)
    assert find_terms("BEST deal", [r"best"], match="regex", case_sensitive=True) == ()


def test_regex_unsafe_term_raises() -> None:
    with pytest.raises(UnsafeRuleRegexError):
        find_terms("aaaa", ["(a+)+"], match="regex", case_sensitive=False)


def test_terms_returned_in_rule_order() -> None:
    assert _word("cure and best", ["best", "cure", "none"]) == ("best", "cure")


# ---- T3: slots -----------------------------------------------------------------------------

FULL = {
    "content": {
        "title": "T",
        "bullets": ["b0", "b1", "b2"],
        "description": "D",
    },
    "keywords": {"search_terms": ["s0"], "hidden_terms": ["h0", "h1"]},
    "attributes": {"color": "black", "sizes": ["s", "m"], "count": 3, "flag": True},
}


def _paths(field: str, kind: Any) -> list[tuple[str, Any]]:
    return iter_slots(listing(**FULL), field, kind=kind)


def test_iter_slots_star_includes_keywords() -> None:
    paths = [p for p, _ in _paths("*", "text")]
    assert paths == [
        "content.title",
        "content.bullets[0]",
        "content.bullets[1]",
        "content.bullets[2]",
        "content.description",
        "keywords.search_terms[0]",
        "keywords.hidden_terms[0]",
        "keywords.hidden_terms[1]",
    ]


def test_iter_slots_bullets_star() -> None:
    assert _paths("content.bullets[*]", "text") == [
        ("content.bullets[0]", "b0"),
        ("content.bullets[1]", "b1"),
        ("content.bullets[2]", "b2"),
    ]
    assert _paths("content.bullets[*]", "length") == _paths("content.bullets[*]", "text")


def test_iter_slots_missing_index_empty() -> None:
    assert _paths("content.bullets[7]", "text") == []
    assert _paths("keywords.search_terms[3]", "length") == []


def test_iter_slots_unindexed_list_expands() -> None:
    assert _paths("content.bullets", "text")[1] == ("content.bullets[1]", "b1")
    assert _paths("keywords.search_terms", "text") == [("keywords.search_terms[0]", "s0")]
    assert _paths("content.bullets", "length") == [("content.bullets", ["b0", "b1", "b2"])]


def test_iter_slots_list_attribute_items() -> None:
    assert _paths("attributes.sizes", "text") == [
        ("attributes.sizes", "s"),
        ("attributes.sizes", "m"),
    ]
    assert _paths("attributes.sizes", "length") == [("attributes.sizes", ["s", "m"])]


def test_iter_slots_non_text_no_slot() -> None:
    for field in ("attributes.count", "attributes.flag", "images[0]", "attributes", "listing"):
        assert _paths(field, "text") == []
        assert _paths(field, "length") == []


# One sample per FIELD_PATH_PATTERNS family and RULE_FIELD_EXTRAS member, with the expected
# slot paths for (text, length).
MATRIX: list[tuple[str, list[str], list[str]]] = [
    ("listing", [], []),
    ("request", [], []),
    ("file.header", [], []),
    ("file.row", [], []),
    ("content.title", ["content.title"], ["content.title"]),
    (
        "content.bullets",
        ["content.bullets[0]", "content.bullets[1]", "content.bullets[2]"],
        ["content.bullets"],
    ),
    ("content.bullets[1]", ["content.bullets[1]"], ["content.bullets[1]"]),
    ("content.description", ["content.description"], ["content.description"]),
    ("keywords.search_terms", ["keywords.search_terms[0]"], ["keywords.search_terms"]),
    ("keywords.search_terms[0]", ["keywords.search_terms[0]"], ["keywords.search_terms[0]"]),
    (
        "keywords.hidden_terms",
        ["keywords.hidden_terms[0]", "keywords.hidden_terms[1]"],
        ["keywords.hidden_terms"],
    ),
    ("keywords.hidden_terms[1]", ["keywords.hidden_terms[1]"], ["keywords.hidden_terms[1]"]),
    ("attributes", [], []),
    ("attributes.color", ["attributes.color"], ["attributes.color"]),
    ("images[0]", [], []),
    ("facts.mrp", [], []),
    ("variants", [], []),
    ("variants[a]", [], []),
    ("variants[a].facts.mrp", [], []),
    ("brand.name", [], []),
    ("*", [p for p, _ in iter_slots(listing(**FULL), "*", kind="text")], None),  # type: ignore[list-item]
    (
        "content.bullets[*]",
        ["content.bullets[0]", "content.bullets[1]", "content.bullets[2]"],
        ["content.bullets[0]", "content.bullets[1]", "content.bullets[2]"],
    ),
    ("images", [], []),
]


@pytest.mark.parametrize(("field", "text_paths", "length_paths"), MATRIX)
def test_iter_slots_field_matrix(
    field: str, text_paths: list[str], length_paths: list[str] | None
) -> None:
    assert [p for p, _ in _paths(field, "text")] == text_paths
    expected_length = text_paths if length_paths is None else length_paths
    assert [p for p, _ in _paths(field, "length")] == expected_length


def test_iter_slots_field_matrix_covers_every_family() -> None:
    samples = [field for field, _, _ in MATRIX]
    for pattern in FIELD_PATH_PATTERNS:
        assert any(re.fullmatch(pattern, s) for s in samples), pattern
    for extra in RULE_FIELD_EXTRAS:
        assert extra in samples


# ---- T3: value access ----------------------------------------------------------------------


def test_lookup_product_account_listing() -> None:
    item = listing(**FULL)
    product = {"gender": "women", "none": None}
    account = {"settings": {"gst": True}}
    assert lookup(item, "product.gender", product=product, account=account) == (True, "women")
    assert lookup(item, "product.none", product=product, account=account) == (True, None)
    assert lookup(item, "product.x", product=product, account=account) == (False, None)
    assert lookup(item, "product.x", product=None, account=None) == (False, None)
    assert lookup(item, "account.settings.gst", product=product, account=account) == (True, True)
    assert lookup(item, "account.settings.x", product=product, account=account) == (False, None)
    assert lookup(item, "account.settings.gst.x", product=product, account=account) == (
        False,
        None,
    )
    assert lookup(item, "content.title", product=None, account=None) == (True, "T")
    assert lookup(item, "attributes.color", product=None, account=None) == (True, "black")
    assert lookup(item, "attributes.missing", product=None, account=None) == (False, None)
    assert lookup(item, "content.bullets[9]", product=None, account=None) == (False, None)


@pytest.mark.parametrize(
    "path",
    [
        "facts.mrp",
        "brand.name",
        "variants",
        "variants[a]",
        "variants[a].facts.x",
        "request",
        "file.header",
        "file.row",
        "listing",
    ],
)
def test_lookup_not_listing_roots_not_found(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object) -> object:
        raise AssertionError("resolve must not be called")

    monkeypatch.setattr(text_mod, "resolve", boom)
    assert lookup(listing(), path, product=None, account=None) == (False, None)


@pytest.mark.parametrize("error", [KeyError, IndexError, ValueError, TypeError])
def test_lookup_resolve_errors_not_found(
    error: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def raises(*_a: object) -> object:
        raise error("x")

    monkeypatch.setattr(text_mod, "resolve", raises)
    assert lookup(listing(), "content.title", product=None, account=None) == (False, None)


@pytest.mark.parametrize(
    ("value", "present"),
    [
        (None, False),
        ("", False),
        ("  ", False),
        ([], False),
        ([" "], False),
        (False, True),
        (0, True),
        ("x", True),
        (["x"], True),
    ],
)
def test_is_present_table(value: object, present: bool) -> None:
    assert is_present(value) is present


def _cond(**data: Any) -> Condition:
    return Condition.model_validate(data)


@pytest.mark.parametrize(
    ("cond", "product", "holds"),
    [
        ({"path": "product.g", "op": "present"}, {"g": "x"}, True),
        ({"path": "product.g", "op": "present"}, {"g": " "}, False),
        ({"path": "product.g", "op": "present"}, {}, False),
        ({"path": "product.g", "op": "absent"}, {}, True),
        ({"path": "product.g", "op": "absent"}, {"g": None}, True),
        ({"path": "product.g", "op": "absent"}, {"g": 0}, False),
        ({"path": "product.g", "op": "eq", "value": "Women"}, {"g": " women "}, True),
        ({"path": "product.g", "op": "eq", "value": "men"}, {"g": "women"}, False),
        ({"path": "product.g", "op": "eq", "value": "x"}, {}, False),
        ({"path": "product.g", "op": "eq", "value": "x"}, {"g": None}, False),
        ({"path": "product.g", "op": "ne", "value": "x"}, {}, False),
        ({"path": "product.g", "op": "ne", "value": "x"}, {"g": "y"}, True),
        ({"path": "product.g", "op": "ne", "value": "x"}, {"g": "X"}, False),
        ({"path": "product.g", "op": "eq", "value": 1}, {"g": True}, False),
        ({"path": "product.g", "op": "eq", "value": True}, {"g": 1}, False),
        ({"path": "product.g", "op": "eq", "value": True}, {"g": True}, True),
        ({"path": "product.g", "op": "eq", "value": 2}, {"g": 2.0}, True),
        ({"path": "product.g", "op": "eq", "value": "2"}, {"g": 2}, False),
        ({"path": "product.g", "op": "in", "values": ["a", "B"]}, {"g": "b"}, True),
        ({"path": "product.g", "op": "in", "values": ["a"]}, {"g": "c"}, False),
        ({"path": "product.g", "op": "in", "values": ["a"]}, {}, False),
        ({"path": "product.g", "op": "not_in", "values": ["a"]}, {"g": "c"}, True),
        ({"path": "product.g", "op": "not_in", "values": ["a"]}, {"g": "A"}, False),
        ({"path": "product.g", "op": "not_in", "values": ["a"]}, {}, False),
        ({"path": "product.g", "op": "eq", "value": "m"}, {"g": ["s", "m"]}, True),
    ],
)
def test_condition_ops_table(cond: dict[str, Any], product: dict[str, object], holds: bool) -> None:
    assert condition_holds(_cond(**cond), listing(), product=product, account=None) is holds


def test_condition_listing_path() -> None:
    cond = _cond(path="attributes.color", op="eq", value="BLACK")
    assert condition_holds(cond, listing(), product=None, account=None) is True
