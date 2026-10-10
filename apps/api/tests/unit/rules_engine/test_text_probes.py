"""The regex guard's probe builders in rules_engine/text.py, called directly (S26 T23 gate-fix;
AC 11). They run once at import to build module constants, so only a direct call shows what
each one returns; the guard suites (test_regex_guard*.py) use the constants they build."""

from __future__ import annotations

import re

from app.services.rules_engine import text as text_mod

MAX = 0x10FFFF


def test_around_takes_each_point_and_its_neighbours_in_order() -> None:
    assert text_mod._around({5}) == "\x04\x05\x06"
    assert text_mod._around({9, 5, 6}) == "".join(map(chr, (4, 5, 6, 7, 8, 9, 10)))
    assert text_mod._around(set()) == ""


def test_around_keeps_both_ends_of_the_code_space() -> None:
    assert text_mod._around({0}) == "\x00\x01"
    assert text_mod._around({MAX}) == chr(MAX - 1) + chr(MAX)


def test_category_probe_text_holds_the_run_ends() -> None:
    probes = text_mod._category_probe_text()
    points = [ord(c) for c in probes]
    assert points == sorted(set(points))
    # \d and \w runs over ASCII digits end at "0" and after "9"; \s at TAB..CR, FS..SPACE, U+3000
    for end in (0x30, 0x3A, 0x09, 0x0E, 0x1C, 0x21, 0x3000, 0x3001):
        assert {end - 1, end, end + 1} <= set(points), hex(end)
    # the middle of a run and of a gap is not probed
    assert ord("5") not in points
    assert ord("m") not in points
    assert probes == text_mod._CATEGORY_PROBE_TEXT


def test_cased_points() -> None:
    cased = text_mod._cased_points()
    assert isinstance(cased, frozenset)
    assert all(isinstance(p, int) for p in cased)
    # a, A, z, k, KELVIN SIGN, LONG S, SHARP S, CAPITAL SHARP S, YPOGEGRAMMENI
    for point in (0x61, 0x41, 0x7A, 0x6B, 0x212A, 0x17F, 0xDF, 0x1E9E, 0x345):
        assert point in cased, hex(point)
    # 0, space, underscore, IDEOGRAPHIC SPACE, the last code point
    for point in (0x30, 0x20, 0x5F, 0x3000, MAX):
        assert point not in cased, hex(point)
    assert cased == text_mod._CASED


def test_fold_probe_text_is_the_cased_points_and_their_neighbours() -> None:
    probes = text_mod._fold_probe_text()
    assert probes == text_mod._around(set(text_mod._CASED))
    assert {"`", "a", "b", "z", "{"} <= set(probes)
    assert "5" not in probes


def test_refused_error_default_reason() -> None:
    error = text_mod._RefusedError()
    reason = "adjacent overlapping variable-width repeats can backtrack polynomially"
    assert (error.reason, str(error)) == (reason, reason)
    assert str(text_mod._RefusedError("other")) == "other"


def test_sub_flags_unicode_group_clears_only_ascii() -> None:
    flags = re.ASCII | re.IGNORECASE
    assert text_mod._sub_flags(flags, (None, re.UNICODE, 0)) == re.IGNORECASE
    assert text_mod._sub_flags(re.IGNORECASE, (None, re.DOTALL, re.IGNORECASE)) == re.DOTALL


def test_condition_on_a_null_value_is_false_for_every_value_op() -> None:
    """D10b: a value op on a path that resolves to null (a product field set to None) is false;
    only `present`/`absent` look at presence."""
    from mkos_schemas.rules import Condition
    from tests.rules_engine_builders import listing

    product = {"gender": None}
    for op, extra in (("ne", {"value": "men"}), ("not_in", {"values": ["men"]})):
        cond = Condition.model_validate({"path": "product.gender", "op": op, **extra})
        assert not text_mod.condition_holds(cond, listing(), product=product, account=None), op
    absent = Condition.model_validate({"path": "product.gender", "op": "absent"})
    assert text_mod.condition_holds(absent, listing(), product=product, account=None)
