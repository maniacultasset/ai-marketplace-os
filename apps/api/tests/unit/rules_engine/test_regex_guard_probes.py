"""S14a fix round 3 (R3-1): the overlap guard decides atom overlap exactly. Every atom's match set
is a union of code-point intervals; two such sets meet exactly when one holds the left end of an
interval of the other. The guard probes those ends: range and literal neighbours, the interval
ends of the Unicode categories, and (under IGNORECASE) every cased character and its neighbours.
An atom whose mask is 0 overlaps everything (fail closed).
Moved from the round 3 tester repro."""

from __future__ import annotations

import re
import time
from functools import lru_cache

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.rules_engine_builders import listing, rule, ruleset

# Matches U+4E00.. (CJK) but none of the old guard's fixed probes.
_HOLE = r"[^\x00-\u4dff\uff21\U0001f600]"


def _refused(regex: str, *, ignore_case: bool = False) -> bool:
    try:
        compile_rule_regex(regex, ignore_case=ignore_case)
    except UnsafeRuleRegexError:
        return True
    return False


def test_control_plain_cjk_class_is_refused() -> None:
    assert _refused("[一-鿿]*" * 3 + "x")


@pytest.mark.parametrize("ignore_case", [False, True])
@pytest.mark.parametrize("k", [2, 3, 7])  # 7 copies = 218 chars, under the 256 limit
def test_negated_class_outside_probes_is_refused(k: int, ignore_case: bool) -> None:
    regex = (_HOLE + "*") * k + "x"
    assert len(regex) <= 256
    assert _refused(regex, ignore_case=ignore_case)


def test_hole_regex_refused_by_evaluate_without_stalling() -> None:
    regex = (_HOLE + "*") * 4 + "x"
    pattern_rule = rule(
        {"type": "pattern", "name": "n", "regex": regex, "must": "not_match"},
        field="content.description",
    )
    banned_rule = rule({"type": "banned_terms", "terms": [regex], "match": "regex"}, field="*")
    for r in (pattern_rule, banned_rule):
        start = time.perf_counter()
        with pytest.raises(UnsafeRuleRegexError):  # was accepted: ~0.5 s for 100 chars
            evaluate(listing(content={"description": "一" * 100}), ruleset(r))
        assert time.perf_counter() - start < 0.1


@pytest.mark.parametrize(
    "regex",
    [
        # Osmanya digits (U+104A0..) are \d; U+1FBF0.. and U+20000.. are \w. No range end
        # or fixed probe lands there: only the category run ends do.
        r"[^\x00-\U00010400]*\d*x",
        r"\d*[^\x00-\U00010400]*x",
        r"[^\x00-\U0001f700]*\w*x",
        r"[^\x00-\U0001f700\W]*[\w]*x",
    ],
)
def test_category_overlap_outside_fixed_probes_is_refused(regex: str) -> None:
    assert _refused(regex)


def test_negated_classes_meeting_past_a_range_end_are_refused() -> None:
    # They share U+4E11..; the second matches "A" (so its mask is not 0) and excludes every
    # fixed probe the first matches, so only the neighbours of its range end and literals
    # (U+4E11, U+FF22, ...) show the overlap.
    assert _refused(r"[^\x00-\u4dff]*[^\x00-\x40\x42-\u4e10\uff21\U0001f600\U0010ffff]*x")


def test_inline_unicode_group_under_global_ascii_is_refused() -> None:
    # (?u:...) re-enables Unicode \w inside a global (?a), so the scoped \w matches é and
    # the repeats overlap; modelling it as ASCII \w accepted it (~2 s on "é" * 3000).
    assert re.fullmatch(r"(?a)(?u:\w)", "é")
    assert _refused(r"(?a)(?u:\w)*é*x")
    assert not _refused(r"(?a)\w*é*x")  # control: ASCII \w cannot match é


def test_empty_class_after_open_repeat_fails_closed() -> None:
    assert _refused(r"x*[^\x00-\U0010ffff]*y")


@pytest.mark.parametrize(
    "regex",
    [
        # (?i:U+03A3) also matches U+03C3, deep inside the case-sensitive range; no own probe
        # lands on U+03C3, only the case-fold probes do.
        r"[\u03b0-\u03d0]*(?i:\u03a3)*x",
        r"(?i:\u03a3)*(?-i:[\u03b0-\u03d0])*x",
    ],
)
def test_case_fold_overlap_is_refused(regex: str) -> None:
    assert _refused(regex)


@pytest.mark.parametrize(
    "regex",
    [
        r"\d+\s+\d+",
        r"[^,]+,[^,]+",
        r"[a-z]+-[0-9]+",
        r"सबसे\s+सस्ता\s*",
        r"[ऀ-ॿ]+\s+[ऀ-ॿ]+",
        r"\w+\s*:\s*\d+",
    ],
)
@pytest.mark.parametrize("ignore_case", [False, True])
def test_disjoint_atoms_still_accepted(regex: str, ignore_case: bool) -> None:
    assert not _refused(regex, ignore_case=ignore_case)


def test_empty_class_fails_closed() -> None:
    # An atom that matches no character gets mask 0; the guard treats it as overlapping.
    assert _refused(r"[^\x00-\U0010ffff]*[^\x00-\U0010ffff]*x")


# ---------------------------------------------------------------------------------------------
# Property: the guard's overlap verdict equals brute-force set intersection.


@lru_cache(maxsize=1)
def _all_code_points() -> str:
    return "".join(map(chr, range(0x110000)))


_ANCHORS = (0x0, 0x41, 0x7F, 0xFF, 0x212A, 0x4E00, 0xD7FF, 0xE000, 0xFF10, 0x1D7CE, 0x10FFFF)


def _near(anchor: int) -> st.SearchStrategy[int]:
    return st.integers(max(0, anchor - 40), min(0x10FFFF, anchor + 40))


_code_points = st.one_of(st.integers(0, 0x10FFFF), st.sampled_from(_ANCHORS).flatmap(_near))


def _esc(cp: int) -> str:
    return f"\\U{cp:08x}"


@st.composite
def _class_items(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["range", "range", "from_zero", "literal", "category"]))
    if kind == "category":
        return draw(st.sampled_from([r"\d", r"\D", r"\w", r"\W", r"\s", r"\S"]))
    if kind == "from_zero":  # negated, it leaves a hole above every fixed probe (R3-1)
        return f"{_esc(0)}-{_esc(draw(_code_points))}"
    lo = draw(_code_points)
    if kind == "literal":
        return _esc(lo)
    hi = draw(st.integers(lo, min(0x10FFFF, lo + draw(st.sampled_from([0, 3, 200, 70000])))))
    return f"{_esc(lo)}-{_esc(hi)}"


@st.composite
def _classes(draw: st.DrawFn) -> str:
    items = draw(st.lists(_class_items(), min_size=1, max_size=3))
    cls = "[" + ("^" if draw(st.booleans()) else "") + "".join(items) + "]"
    return draw(st.sampled_from([cls, cls, f"(?i:{cls})", f"(?-i:{cls})"]))


@settings(
    derandomize=True,
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(_classes(), _classes(), st.booleans())
def test_guard_overlap_matches_brute_force(a: str, b: str, ignore_case: bool) -> None:
    flags = re.IGNORECASE if ignore_case else 0
    space = _all_code_points()
    a_set, b_set = re.compile(a, flags), re.compile(b, flags)
    if a_set.search(space) is None or b_set.search(space) is None:
        return  # an empty class fails closed (test_empty_class_fails_closed)
    overlap = re.compile(f"(?={a}){b}", flags).search(space) is not None
    assert _refused(f"{a}*{b}*x", ignore_case=ignore_case) is overlap
