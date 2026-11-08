"""SI13 T15 (AC15; P:B4, D7; CP-D12): value-map proposals from observed outcomes."""

from __future__ import annotations

from app.services.resolution.value_maps import Observation, propose

ENUMS = {"size": ("S", "M", "L", "2XL", "3XL", "XXXL")}


def _o(
    v: str, w: str | None, n: int, *, key: str = "size", child: bool = False, t: str = "a"
) -> Observation:
    return Observation("amazon", "KURTA", key, v, w, child, f"listing-{n}", t)


def _three(v: str = "XXL", w: str = "2XL") -> list[Observation]:
    return [_o(v, w, n, t="a" if n < 2 else "b") for n in range(3)]


def test_three_listings_make_one_proposal() -> None:
    found, unpaired = propose(_three(), ENUMS, {})
    (p,) = found
    assert (p.key, p.v, p.w, p.listings, p.tenants) == ("size", "XXL", "2XL", 3, 2)
    assert p.alias_line() == '"xxl": ("2XL",),' and unpaired == []


def test_refusals() -> None:
    assert propose(_three()[:2], ENUMS, {})[0] == []
    split = [*_three(), _o("XXL", "XXXL", 9)]
    assert propose(split, ENUMS, {})[0] == []
    assert propose(_three(w="2XLL"), ENUMS, {})[0] == []
    assert propose(_three(), ENUMS, {"xxl": ("2XL",)})[0] == []
    other = [_o("XXL", "2XL", n, key="fabric_type") for n in range(3)]
    assert propose(other, ENUMS, {})[0] == []


def test_unicode_fold_counts_as_one_value() -> None:
    rows = [_o("ｘｘｌ", "2XL", 0), _o("XXL", "2XL", 1), _o(" xxl ", "2XL", 2)]  # noqa: RUF001
    (p,) = propose(rows, ENUMS, {})[0]
    assert p.listings == 3


def test_child_rows_are_unpaired_only() -> None:
    rows = [_o("XXL", None, n, child=True) for n in range(4)]
    found, unpaired = propose(rows, ENUMS, {})
    assert found == [] and [(u.v, u.listings) for u in unpaired] == [("XXL", 4)]


def test_free_text_values_are_ignored() -> None:
    rows = [_o("X" * 65, "2XL", n) for n in range(3)] + [_o("XX\x07L", "2XL", n) for n in range(3)]
    assert propose(rows, ENUMS, {}) == ([], [])
