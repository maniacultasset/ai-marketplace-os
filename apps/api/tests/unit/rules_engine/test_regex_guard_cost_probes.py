"""S14a: the overlap guard's probe tiers (R3-1) stay cheap. Each atom is matched only against
the tier probes that some atom of the same regex matches, so 500 regex banned terms of many
distinct class atoms cost about what they did before the tiers (security review, post-402d888:
5.65 s ignore-case / 2.78 s case-sensitive, cold)."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from app.services.rules_engine import evaluate
from app.services.rules_engine import text as text_mod
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.rules_engine_builders import listing, rule, ruleset
from tests.timing import budget


def _class_atom_term(i: int) -> str:
    # 36 distinct `[\w<cjk>]*-` atoms, 252 characters: every repeat is closed by its `-`.
    return "".join(f"[\\w{chr(0x4E00 + (i * 36 + j) % 20000)}]*-" for j in range(36))


def _cold() -> None:
    text_mod._matcher.cache_clear()
    text_mod._compiled_or_refusal.cache_clear()
    text_mod._tier_mask.cache_clear()
    text_mod._charset_pattern.cache_clear()


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_many_distinct_class_atoms_guard_cost_bounded(case_sensitive: bool) -> None:
    terms = [_class_atom_term(i) for i in range(500)]
    assert all(len(t) == 252 for t in terms)
    r = rule(
        {
            "type": "banned_terms",
            "terms": terms,
            "match": "regex",
            "case_sensitive": case_sensitive,
        },
        field="content.description",
    )
    _cold()
    start = time.perf_counter()
    evaluate(listing(content={"description": "hello"}), ruleset(r))
    # ~0.6 s idle; the pre-fix guard took ~11 s, so 3 s still catches the regression under CI load
    assert time.perf_counter() - start < budget(3.0)


# Exactness: the item-built masks equal scanning the atom's own pattern over each probe tier.

_ANCHORS = (
    *(0x0, 0x41, 0x5A, 0x7F, 0xDF, 0x130, 0x17F, 0x3C2, 0x1E9E, 0x212A, 0x2160, 0x24B6),
    *(0x4E00, 0xD7FF, 0xFB05, 0xFF21, 0x10400, 0x1E900, 0x10FFFF),
)
_points = st.one_of(
    st.integers(0, 0x10FFFF),
    st.sampled_from(_ANCHORS).flatmap(lambda a: st.integers(max(0, a - 40), min(0x10FFFF, a + 40))),
)


def _esc(cp: int) -> str:
    return f"\\U{cp:08x}"


@st.composite
def _atoms(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["class", "class", "class", "literal", "not_literal", "any"]))
    if kind == "any":
        return "."
    if kind != "class":
        return (
            ("" if kind == "literal" else "[^")
            + _esc(draw(_points))
            + ("" if kind == "literal" else "]")
        )
    items = []
    for _ in range(draw(st.integers(1, 3))):
        item = draw(st.sampled_from(["range", "literal", "category"]))
        if item == "category":
            items.append(draw(st.sampled_from([r"\d", r"\D", r"\w", r"\W", r"\s", r"\S"])))
            continue
        lo = draw(_points)
        if item == "literal":
            items.append(_esc(lo))
            continue
        span = draw(st.sampled_from([0, 1, 3, 40, 300, 70000, 0x10FFFF]))
        hi = draw(st.integers(lo, min(0x10FFFF, lo + span)))
        items.append(f"{_esc(lo)}-{_esc(hi)}")
    return "[" + ("^" if draw(st.booleans()) else "") + "".join(items) + "]"


def _atom(source: str, flags: int) -> Any:
    tree = text_mod._sre_parser.parse(source, flags)
    ((op, av),) = list(tree)
    return text_mod._charset(op, av, flags)


@settings(
    derandomize=True,
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    _atoms(),
    st.sampled_from([0, re.IGNORECASE, re.ASCII, re.IGNORECASE | re.ASCII, re.DOTALL]),
    st.lists(_points, max_size=12),
)
def test_item_built_masks_equal_scanning_the_atom(
    source: str, flags: int, extra: list[int]
) -> None:
    atom = _atom(source, flags)
    for i, tier in enumerate(text_mod._TIERS):
        assert text_mod._tier_mask(atom, i) == text_mod._scan_mask(source, flags, tier.text)
    own = text_mod._Tier.of("".join(map(chr, extra)) + "".join(atom.probes))
    built = text_mod._atom_bits(atom, own, lambda op, av: text_mod._item_bits(op, av, flags, own))
    assert built == text_mod._scan_mask(source, flags, own.text)


def test_lowering_keeps_every_category() -> None:
    # IGNORECASE tests a class on the lowered character, so a class's category items may be
    # masked on the character itself only while lowering never changes \w, \d or \s.
    space = "".join(map(chr, range(0x110000)))
    lowered = "".join(chr(text_mod._sre_lib.unicode_tolower(c)) for c in range(0x110000))
    for category in (r"\w", r"\d", r"\s"):
        pattern = re.compile(category)
        assert [m.start() for m in pattern.finditer(space)] == [
            m.start() for m in pattern.finditer(lowered)
        ]


def test_uncased_points_fold_to_themselves() -> None:
    # Outside _CASED a character is its own lower case and no other character lowers to it.
    cased = text_mod._CASED
    for c in range(0x110000):
        low = text_mod._sre_lib.unicode_tolower(c)
        if c not in cased:
            assert low == c
        if low != c:
            assert low in cased
