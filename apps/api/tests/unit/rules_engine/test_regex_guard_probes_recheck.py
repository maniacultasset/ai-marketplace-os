"""S14a tester round 4 re-check of R3-1 (exact atom overlap in the regex guard).

The fix's own property draws bracket classes only. This one widens the atom shapes to every
kind the guard probes: bare literals, single-character negations (``NOT_LITERAL``), ``.`` with and
without DOTALL, bare categories, and inline ASCII / IGNORECASE / mixed scopes on either side.
The guard's verdict for ``A*B*x`` must equal brute-force intersection over all code points."""

from __future__ import annotations

import re
import time
from functools import lru_cache

import pytest
from app.services.rules_engine.text import UnsafeRuleRegexError, compile_rule_regex
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _refused(regex: str, *, ignore_case: bool = False) -> bool:
    try:
        compile_rule_regex(regex, ignore_case=ignore_case)
    except UnsafeRuleRegexError:
        return True
    return False


@lru_cache(maxsize=1)
def _all_code_points() -> str:
    return "".join(map(chr, range(0x110000)))


# Edges of the probe tiers: Latin-1 end, case-fold oddities (Kelvin, long s, dotted I, micro,
# Greek final sigma), category run ends (Osmanya digits, CJK ext. B), surrogates, max.
_ANCHORS = (
    0x0,
    0xA,
    0x41,
    0x5A,
    0x7F,
    0xB5,
    0xFF,
    0x100,
    0x130,
    0x17F,
    0x345,
    0x3C2,
    0x1E9E,
    0x212A,
    0x4E00,
    0x104A0,
    0xD800,
    0xDFFF,
    0x1FBF0,
    0x20000,
    0x10FFFF,
)
_code_points = st.one_of(
    st.integers(0, 0x10FFFF),
    st.sampled_from(_ANCHORS).flatmap(lambda a: st.integers(max(0, a - 3), min(0x10FFFF, a + 3))),
)
_CATEGORIES = (r"\d", r"\D", r"\w", r"\W", r"\s", r"\S")
_SCOPES = ("{}", "(?i:{})", "(?-i:{})", "(?a:{})", "(?ai:{})", "(?s:{})", "(?as:{})")


def _esc(cp: int) -> str:
    return f"\\U{cp:08x}"


@st.composite
def _items(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["range", "literal", "category", "from_zero"]))
    if kind == "category":
        return draw(st.sampled_from(_CATEGORIES))
    lo = draw(_code_points)
    if kind == "literal":
        return _esc(lo)
    if kind == "from_zero":
        return f"{_esc(0)}-{_esc(lo)}"
    hi = draw(st.integers(lo, min(0x10FFFF, lo + draw(st.sampled_from([1, 40, 5000])))))
    return f"{_esc(lo)}-{_esc(hi)}"


@st.composite
def _atoms(draw: st.DrawFn) -> str:
    shape = draw(st.sampled_from(["literal", "not_literal", "dot", "category", "class"]))
    if shape == "literal":
        body = _esc(draw(_code_points))
    elif shape == "not_literal":
        body = f"[^{_esc(draw(_code_points))}]"
    elif shape == "dot":
        body = "."
    elif shape == "category":
        body = draw(st.sampled_from(_CATEGORIES))
    else:
        neg = "^" if draw(st.booleans()) else ""
        body = "[" + neg + "".join(draw(st.lists(_items(), min_size=1, max_size=3))) + "]"
    return draw(st.sampled_from(_SCOPES)).format(body)


@settings(
    derandomize=True,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(_atoms(), _atoms(), st.booleans())
def test_guard_overlap_matches_brute_force_all_atom_shapes(
    a: str, b: str, ignore_case: bool
) -> None:
    flags = re.IGNORECASE if ignore_case else 0
    space = _all_code_points()
    if re.search(a, space, flags) is None or re.search(b, space, flags) is None:
        return  # an empty atom fails closed (test_regex_guard_probes.py)
    overlap = re.search(f"(?={a}){b}", space, flags) is not None
    assert _refused(f"{a}*{b}*x", ignore_case=ignore_case) is overlap, (a, b, ignore_case)


@pytest.mark.parametrize(
    ("regex", "ignore_case"),
    [
        # Kelvin sign folds to k/K only without ASCII.
        (r"\u212a*[k]*x", True),
        (r"(?i:k)*\u212a*x", False),
        # Long s folds to s/S; dotted capital I lowers to i.
        (r"(?i:\u017f)*s*x", False),
        (r"(?i:\u0130)*i*x", False),
        # \w without ASCII holds Osmanya digits; a negated class leaves only them.
        (r"[^\x00-\U000104a0]*\w*x", False),
        # Single-character negations share everything but two code points.
        (r"[^\U0010ffff]*[^\u4e00]*x", False),
        # . (no DOTALL) meets a class that holds only a newline neighbour.
        (r".*[\x0b]*x", False),
    ],
)
def test_case_fold_and_category_edges_refused(regex: str, ignore_case: bool) -> None:
    assert _refused(regex, ignore_case=ignore_case)


@pytest.mark.parametrize(
    ("regex", "ignore_case"),
    [
        # ASCII scope: (?ai:k) takes k and K only, never the Kelvin sign.
        (r"(?ai:k)*\u212a*x", False),
        (r"(?a:\w)*[\u0100-\U0010ffff]*x", False),
        # . without DOTALL never takes a newline.
        (r".*\n*x", False),
    ],
)
def test_ascii_and_dotall_edges_accepted(regex: str, ignore_case: bool) -> None:
    assert not _refused(regex, ignore_case=ignore_case)


def test_guard_cost_with_both_tiers_stays_flat() -> None:
    # Many distinct atoms, categories and IGNORECASE: both import-time tiers are probed.
    regex = "".join(f"(?i:[\\u{0x4E00 + i * 7:04x}\\w])x" for i in range(20))
    start = time.perf_counter()
    for _ in range(5):
        compile_rule_regex(regex + f"{_}", ignore_case=True)
    assert time.perf_counter() - start < 2.0
