"""SI4 A2, A3 (AC2, AC3): the reformat and remap fixers change one slot, safely, or nothing."""

from __future__ import annotations

import json
import unicodedata
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from app.services.auto_fix.fixers import (
    TRAILING_PUNCTUATION,
    FixContext,
    clean_text,
    fix_keyword_duplicates,
    fix_reformat,
    fix_remap_value,
    fold_spelling,
)
from app.services.auto_fix.policy import TRIM_MAX_SHARE
from app.services.rules_engine.template import template_allowed
from app.services.rules_engine.text import measure
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, FixAction, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.listing import CanonicalListing
from tests.rules_engine_builders import listing, rule, ruleset, template

UNIT = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
STUB_212 = (UNIT * 5)[:211] + "."  # the stub's long_title shape (A7)
TITLE_RULE = UUID("51400000-0000-0000-0000-00000000a001")
BULLET_RULE = UUID("51400000-0000-0000-0000-00000000a002")
DESC_RULE = UUID("51400000-0000-0000-0000-00000000a003")
HIDDEN_RULE = UUID("51400000-0000-0000-0000-00000000a004")
MIN_RULE = UUID("51400000-0000-0000-0000-00000000a005")
CRITICAL = frozenset({"fiber_composition", "country_of_origin"})


def _max(rule_id: UUID, field: str, limit: int, unit: str = "chars", **over: Any) -> Any:
    rule_def: dict[str, Any] = {"type": "max_length", "limit": limit, "unit": unit}
    rule_def.update(over.pop("rule_def", {}))
    return rule(rule_def, id=rule_id, field=field, **over)


def _ctx(
    *rules: Any, tmpl: Any = None, critical: frozenset[str] = CRITICAL, **kw: Any
) -> FixContext:
    if not rules:
        rules = (
            _max(TITLE_RULE, "content.title", 200),
            _max(BULLET_RULE, "content.bullets[*]", 50),
            _max(DESC_RULE, "content.description", 40),
            _max(HIDDEN_RULE, "keywords.hidden_terms", 250, "bytes"),
        )
    return FixContext(ruleset=ruleset(*rules), template=tmpl, critical_keys=critical, **kw)


def _item(
    field: str, rule_id: UUID | None, code: ErrorCode = ErrorCode.FIELD_TOO_LONG
) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=code,
        severity=Severity.ERROR,
        message="too long",
        source=ErrorSource.RULES_ENGINE,
        retryable=True,
        rule_id=rule_id,
    )


def _dump_without(item: CanonicalListing, *paths: tuple[str, ...]) -> str:
    data = item.model_dump(mode="json")
    for path in paths:
        node = data
        for part in path[:-1]:
            node = node[part]
        node.pop(path[-1], None)
    return json.dumps(data, sort_keys=True)


# ---- reformat: happy paths ----


def test_stub_title_is_cut_at_a_word_boundary() -> None:
    assert len(STUB_212) == 212
    base = listing(content={"title": STUB_212})
    got = fix_reformat(base, _item("content.title", TITLE_RULE), _ctx())
    assert got is not None
    fixed, record = got
    title = fixed.content.title
    assert len(title) <= 200
    assert STUB_212.startswith(title)
    assert STUB_212[len(title)] == " "
    assert not title.endswith(tuple(TRAILING_PUNCTUATION))
    assert (record.before_size, record.after_size, record.unit) == (212, len(title), "chars")
    assert record.fix_action is FixAction.REFORMAT
    assert record.rule_id == TITLE_RULE
    assert record.before_sha != record.after_sha
    assert len(record.before_sha) == 64
    assert title not in record.action_taken
    assert "title" in record.action_taken
    assert _dump_without(fixed, ("content", "title")) == _dump_without(base, ("content", "title"))


def test_a_cut_never_ends_on_a_connector_word() -> None:
    """QA: the 212-character stub title came back ending "... Cotton Kurta Set for"."""
    base = listing(content={"title": STUB_212})
    got = fix_reformat(base, _item("content.title", TITLE_RULE), _ctx())
    assert got is not None
    title = got[0].content.title
    assert title.rsplit(" ", 1)[-1].casefold() not in {"for", "with", "and", "of", "the"}
    assert title.endswith("Kurta Set")


def test_bullet_is_fixed_alone() -> None:
    bullets = [
        "Soft cotton fabric",
        "Machine washable",
        "A breathable cotton weave for summer days",
    ]
    bullets[2] = bullets[2] + " ok"  # 44 chars
    base = listing(content={"bullets": bullets})
    ctx = _ctx(_max(BULLET_RULE, "content.bullets[*]", 41))
    got = fix_reformat(base, _item("content.bullets[2]", BULLET_RULE), ctx)
    assert got is not None
    fixed, record = got
    assert fixed.content.bullets[:2] == bullets[:2]
    assert fixed.content.bullets[2] == "A breathable cotton weave for summer days"
    assert "bullet point 3" in record.action_taken


def test_description_is_fixed() -> None:
    base = listing(content={"description": "A comfortable cotton kurta set for daily wear"})
    got = fix_reformat(base, _item("content.description", DESC_RULE), _ctx())
    assert got is not None
    assert got[0].content.description == "A comfortable cotton kurta set for daily"


def test_hidden_terms_over_the_byte_budget() -> None:
    terms = [f"term{i:02d} cotton kurta" for i in range(12)]  # 12 * 18 + 11 = 227 bytes
    terms.append("extra words that overflow it")  # 227 + 29 = 256 bytes
    base = listing(keywords={"hidden_terms": terms})
    item = _item("keywords.hidden_terms", HIDDEN_RULE, ErrorCode.BYTE_BUDGET_EXCEEDED)
    got = fix_reformat(base, item, _ctx())
    assert got is not None
    fixed, record = got
    assert fixed.keywords.hidden_terms == terms[:-1]
    assert measure(fixed.keywords.hidden_terms, "bytes") <= 250
    assert record.unit == "bytes"


def test_hidden_terms_repeats_go_first() -> None:
    terms = [f"term{i:02d} cotton kurta" for i in range(12)] + ["TERM00 cotton kurta "]
    terms.append("x" * 10)
    base = listing(keywords={"hidden_terms": terms})
    item = _item("keywords.hidden_terms", HIDDEN_RULE, ErrorCode.BYTE_BUDGET_EXCEEDED)
    got = fix_reformat(base, item, _ctx())
    assert got is not None
    assert got[0].keywords.hidden_terms == [*terms[:12], "x" * 10]
    assert "repeated term" in got[1].action_taken


def _dedupe_cleaned(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for term in (clean_text(t) for t in terms):
        if term and term.strip().casefold() not in seen:
            seen.add(term.strip().casefold())
            out.append(term)
    return out


def test_repeats_do_not_widen_the_trim_share() -> None:
    """Round 3 F1: the D5 share is measured on the de-duplicated list, so 45 repeats of one
    term cannot hide the loss of most of the unique terms."""
    unique = [f"uniq{i:02d}word" for i in range(5)]  # 5 terms of 10 chars
    terms = ["filler term"] * 45 + unique
    ctx = _ctx(_max(HIDDEN_RULE, "keywords.hidden_terms", 40))
    got = fix_reformat(
        listing(keywords={"hidden_terms": terms}), _item("keywords.hidden_terms", HIDDEN_RULE), ctx
    )
    assert got is None  # dropping 3 of 6 unique terms is far over the 15% share


def test_dedupe_alone_fitting_is_still_fixed() -> None:
    terms = ["filler term"] * 45 + ["uniq00word"]
    ctx = _ctx(_max(HIDDEN_RULE, "keywords.hidden_terms", 40))
    got = fix_reformat(
        listing(keywords={"hidden_terms": terms}), _item("keywords.hidden_terms", HIDDEN_RULE), ctx
    )
    assert got is not None
    fixed, record = got
    assert fixed.keywords.hidden_terms == ["filler term", "uniq00word"]
    assert record.before_size == measure(terms, "chars")  # the raw size stays in the record


# ---- reformat: boundaries ----


def test_exactly_at_the_limit_is_left_alone() -> None:
    base = listing(content={"title": "a " * 99 + "bb"})  # 200 chars
    assert fix_reformat(base, _item("content.title", TITLE_RULE), _ctx()) is None


def test_one_word_over_the_limit_has_no_boundary() -> None:
    base = listing(content={"title": "x" * 201})
    assert fix_reformat(base, _item("content.title", TITLE_RULE), _ctx()) is None


def _share_title(total: int, keep: int) -> str:
    """A title of ``total`` chars whose last space sits at ``keep``."""
    head = ("w " * keep)[:keep]
    head = head[:-1] + "w" if head.endswith(" ") else head
    return head + " " + "z" * (total - keep - 1)


def test_share_of_sixteen_percent_is_refused() -> None:
    title = _share_title(100, 84)  # cut at 84 removes 16
    ctx = _ctx(_max(TITLE_RULE, "content.title", 90))
    base = listing(content={"title": title})
    assert fix_reformat(base, _item("content.title", TITLE_RULE), ctx) is None


def test_share_of_fifteen_percent_is_fixed() -> None:
    title = _share_title(100, 85)  # cut at 85 removes exactly 15
    ctx = _ctx(_max(TITLE_RULE, "content.title", 90))
    base = listing(content={"title": title})
    got = fix_reformat(base, _item("content.title", TITLE_RULE), ctx)
    assert got is not None
    assert len(got[0].content.title) == 85


def test_blank_after_the_trim_is_refused() -> None:
    title = "- " + "x" * 30
    ctx = _ctx(_max(TITLE_RULE, "content.title", 20))
    assert (
        fix_reformat(listing(content={"title": title}), _item("content.title", TITLE_RULE), ctx)
        is None
    )


@pytest.mark.parametrize("unit", ["chars", "bytes"])
def test_devanagari_and_emoji_titles(unit: str) -> None:
    word = "कुर्ता"  # combining vowel signs
    title = " ".join([word] * 40 + ["😀"] * 5)
    limit = int(measure(title, unit) * 0.9)  # type: ignore[arg-type]
    ctx = _ctx(_max(TITLE_RULE, "content.title", limit, unit))
    got = fix_reformat(listing(content={"title": title}), _item("content.title", TITLE_RULE), ctx)
    assert got is not None
    fixed = got[0].content.title
    assert measure(fixed, unit) <= limit  # type: ignore[arg-type]
    assert all(part in {word, "😀"} for part in fixed.split(" "))  # no mark split
    assert unicodedata.is_normalized("NFC", fixed)


def test_control_characters_and_space_runs_are_cleaned() -> None:
    title = "Cotton\u0007 kurta   set\t\tfor  women " + "x" * 3
    ctx = _ctx(_max(TITLE_RULE, "content.title", 30))
    got = fix_reformat(listing(content={"title": title}), _item("content.title", TITLE_RULE), ctx)
    assert got is not None
    assert got[0].content.title == "Cotton kurta set for women xxx"
    assert "Cleaned up spacing" in got[1].action_taken


def test_an_empty_keyword_list_is_left_alone() -> None:
    item = _item("keywords.hidden_terms", HIDDEN_RULE, ErrorCode.BYTE_BUDGET_EXCEEDED)
    assert fix_reformat(listing(), item, _ctx()) is None


def test_a_rule_not_in_the_ruleset() -> None:
    base = listing(content={"title": STUB_212})
    item = _item("content.title", UUID("51400000-0000-0000-0000-00000000ffff"))
    assert fix_reformat(base, item, _ctx()) is None
    assert fix_reformat(base, _item("content.title", None), _ctx()) is None


def test_a_min_length_rule() -> None:
    min_rule = rule({"type": "min_length", "limit": 300}, id=MIN_RULE, field="content.title")
    base = listing(content={"title": STUB_212})
    assert fix_reformat(base, _item("content.title", MIN_RULE), _ctx(min_rule)) is None


def test_another_path_is_not_a_slot() -> None:
    item = _item("attributes.color", TITLE_RULE)
    assert fix_reformat(listing(), item, _ctx()) is None


def test_the_tightest_limit_on_the_slot_wins() -> None:
    other = UUID("51400000-0000-0000-0000-00000000a006")
    ctx = _ctx(_max(TITLE_RULE, "content.title", 200), _max(other, "content.title", 190))
    got = fix_reformat(
        listing(content={"title": STUB_212}), _item("content.title", TITLE_RULE), ctx
    )
    assert got is not None
    assert len(got[0].content.title) <= 190


# ---- keyword duplicates ----


def test_keyword_duplicates_keep_the_first_spelling() -> None:
    base = listing(keywords={"search_terms": ["Kurta", "set", "kurta ", "SET", "women"]})
    item = _item("keywords.search_terms", None, ErrorCode.KEYWORD_DUPLICATE)
    got = fix_keyword_duplicates(base, item, _ctx())
    assert got is not None
    assert got[0].keywords.search_terms == ["Kurta", "set", "women"]
    assert got[1].unit == "terms"


def test_keyword_duplicates_none_when_nothing_repeats() -> None:
    base = listing(keywords={"search_terms": ["kurta", "set"]})
    item = _item("keywords.search_terms", None, ErrorCode.KEYWORD_DUPLICATE)
    assert fix_keyword_duplicates(base, item, _ctx()) is None
    assert fix_keyword_duplicates(base, _item("content.title", None), _ctx()) is None


# ---- properties (AC2) ----

_WORD = st.text(alphabet=st.characters(categories=("L", "N", "M")), min_size=1, max_size=12)


@settings(max_examples=150, deadline=None)
@given(words=st.lists(_WORD, min_size=2, max_size=60), limit=st.integers(10, 300))
def test_title_trim_properties(words: list[str], limit: int) -> None:
    title = clean_text(" ".join(words))[:500].strip()
    if not title or not title.strip():
        return
    ctx = _ctx(_max(TITLE_RULE, "content.title", limit))
    base = listing(content={"title": title})
    got = fix_reformat(base, _item("content.title", TITLE_RULE), ctx)
    if got is None:
        return
    fixed, _record = got
    out = fixed.content.title
    assert measure(out, "chars") <= limit
    cleaned = clean_text(title)
    assert cleaned.startswith(out)
    rest = cleaned[len(out) :]
    assert rest == "" or rest[0] in TRAILING_PUNCTUATION
    assert _dump_without(fixed, ("content", "title")) == _dump_without(base, ("content", "title"))
    assert fix_reformat(fixed, _item("content.title", TITLE_RULE), ctx) is None  # idempotent


_TERM = st.text(alphabet="abcABC ", min_size=1, max_size=8).filter(lambda t: t.strip() != "")


@settings(max_examples=150, deadline=None)
@given(terms=st.lists(_TERM, max_size=40), limit=st.integers(5, 120))
def test_keyword_properties(terms: list[str], limit: int) -> None:
    ctx = _ctx(_max(HIDDEN_RULE, "keywords.hidden_terms", limit, "bytes"))
    base = listing(keywords={"hidden_terms": terms})
    item = _item("keywords.hidden_terms", HIDDEN_RULE, ErrorCode.BYTE_BUDGET_EXCEEDED)
    for fixer in (fix_reformat, fix_keyword_duplicates):
        got = fixer(base, item, ctx)
        if got is None:
            continue
        fixed, _record = got
        out = fixed.keywords.hidden_terms
        cleaned = [clean_text(t) for t in terms]
        pos = 0
        for term in out:  # a subsequence of the (cleaned) input
            while clean_text(cleaned[pos]) != clean_text(term):
                pos += 1
            pos += 1
        if fixer is fix_reformat:
            assert measure(out, "bytes") <= limit
        assert _dump_without(fixed, ("keywords", "hidden_terms")) == _dump_without(
            base, ("keywords", "hidden_terms")
        )
        assert fixer(fixed, item, ctx) is None  # idempotent


@settings(max_examples=150, deadline=None)
@given(
    head=st.lists(_TERM, min_size=1, max_size=4),
    repeats=st.integers(0, 34),  # the slot holds at most 50 terms
    tail=st.lists(_TERM, max_size=12),
    limit=st.integers(5, 80),
)
def test_keyword_trim_share_ignores_repeats(
    head: list[str], repeats: int, tail: list[str], limit: int
) -> None:
    """Round 3 F1: whatever the repeats, a trim drops at most the D5 share of the
    de-duplicated content."""
    terms = [*head, *(head[:1] * repeats), *tail]
    ctx = _ctx(_max(HIDDEN_RULE, "keywords.hidden_terms", limit, "bytes"))
    item = _item("keywords.hidden_terms", HIDDEN_RULE, ErrorCode.BYTE_BUDGET_EXCEEDED)
    got = fix_reformat(listing(keywords={"hidden_terms": terms}), item, ctx)
    if got is None:
        return
    kept = got[0].keywords.hidden_terms
    deduped = measure(_dedupe_cleaned(terms), "bytes")
    assert Decimal(deduped - measure(kept, "bytes")) <= TRIM_MAX_SHARE * deduped


# ---- remap_value (A3) ----


def _fit_template(
    *allowed: str, value_map: dict[str, str] | None = None, value_type: str = "enum"
) -> Any:
    spec: dict[str, Any] = {"requirement": "optional", "value_type": value_type}
    spec["allowed_values"] = list(allowed)
    if value_map:
        spec["value_map"] = value_map
    return template(specs={"fit": spec, "fiber_composition": spec, "color": spec})


def _remap(attrs: dict[str, Any], tmpl: Any, key: str = "fit", **over: Any) -> Any:
    base = listing(attributes=attrs, **over)
    item = _item(f"attributes.{key}", None, ErrorCode.VALUE_NOT_ALLOWED)
    return fix_remap_value(base, item, _ctx(tmpl=tmpl))


@pytest.mark.parametrize("value", ["straight-fit", "STRAIGHT_FIT ", "Straight   fit"])
def test_remap_to_the_allowed_spelling(value: str) -> None:
    got = _remap({"fit": value}, _fit_template("Straight Fit", "Slim Fit"))
    assert got is not None
    fixed, record = got
    assert fixed.attributes["fit"] == "Straight Fit"
    assert record.fix_action is FixAction.REMAP_VALUE
    assert value.strip() not in record.action_taken


def test_remap_to_a_value_map_key() -> None:
    got = _remap({"fit": "regular-fit"}, _fit_template("Slim Fit", value_map={"regular_fit": "R"}))
    assert got is not None
    assert got[0].attributes["fit"] == "regular_fit"


def test_ambiguous_spellings_are_refused() -> None:
    assert _remap({"fit": "a_b"}, _fit_template("A-B", "A B")) is None


def test_no_match_is_refused() -> None:
    assert _remap({"fit": "zigzag"}, _fit_template("Straight Fit")) is None


def test_a_critical_key_is_never_remapped() -> None:
    tmpl = _fit_template("Cotton 100%")
    assert _remap({"fiber_composition": "cotton-100%"}, tmpl, key="fiber_composition") is None


def test_a_fact_sourced_value_is_never_remapped() -> None:
    tmpl = _fit_template("Straight Fit")
    got = _remap({"fit": "straight-fit"}, tmpl, attribute_sources={"fit": "attributes.fit"})
    assert got is None


def test_an_overlay_key_is_never_remapped() -> None:
    base = listing(attributes={"fit": "straight-fit"})
    item = _item("attributes.fit", None, ErrorCode.VALUE_NOT_ALLOWED)
    ctx = _ctx(tmpl=_fit_template("Straight Fit"), overlay_keys=frozenset({"fit"}))
    assert fix_remap_value(base, item, ctx) is None


def test_no_template_or_a_non_enum_spec() -> None:
    assert _remap({"fit": "straight-fit"}, None) is None
    assert _remap({"fit": "straight-fit"}, _fit_template("Straight Fit", value_type="text")) is None


def test_a_list_keeps_its_order() -> None:
    tmpl = _fit_template("Navy Blue", "Red")
    got = _remap({"color": ["navy-blue", "Red"]}, tmpl, key="color")
    assert got is not None
    assert got[0].attributes["color"] == ["Navy Blue", "Red"]
    assert got[1].after_size == 1


def test_a_unicode_value() -> None:
    got = _remap({"fit": "सीधा-फिट"}, _fit_template("सीधा फिट"))
    assert got is not None
    assert got[0].attributes["fit"] == "सीधा फिट"


def test_already_exact_is_left_alone() -> None:
    assert _remap({"fit": "Straight Fit"}, _fit_template("Straight Fit")) is None


def test_another_code_is_not_remapped() -> None:
    base = listing(attributes={"fit": "straight-fit"})
    item = _item("attributes.fit", None, ErrorCode.MISSING_ATTRIBUTE)
    assert fix_remap_value(base, item, _ctx(tmpl=_fit_template("Straight Fit"))) is None


_SPELLING = st.sampled_from(["Straight Fit", "Slim Fit", "Regular", "A-Line", "Kurta_Set"])


@settings(max_examples=100, deadline=None)
@given(choice=_SPELLING, noise=st.sampled_from(["", " ", "-", "_"]), upper=st.booleans())
def test_remap_output_is_always_allowed(choice: str, noise: str, upper: bool) -> None:
    tmpl = _fit_template("Straight Fit", "Slim Fit", "Regular", "A-Line", "Kurta_Set")
    raw = choice.replace(" ", noise or " ")
    raw = raw.upper() if upper else raw.lower()
    got = _remap({"fit": raw}, tmpl)
    allowed = template_allowed(tmpl, "fit") or []
    if got is None:
        assert raw in allowed or fold_spelling(raw) != fold_spelling(choice)
        return
    assert got[0].attributes["fit"] in allowed
