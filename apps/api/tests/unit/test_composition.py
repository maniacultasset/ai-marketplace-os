"""SI1 T2: the pure fabric composition parser (AC2, D1, D9(a))."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.services.composition import CompositionError, Fibre, normalized, parse_composition
from hypothesis import given
from hypothesis import strategies as st


def _pairs(text: str) -> list[tuple[str, Decimal]]:
    return [(f.fibre, f.percent) for f in parse_composition(text)]


# ---- happy paths ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("60% Cotton, 40% Polyester", [("Cotton", Decimal(60)), ("Polyester", Decimal(40))]),
        ("Cotton 60%, Polyester 40%", [("Cotton", Decimal(60)), ("Polyester", Decimal(40))]),
        ("100% cotton", [("cotton", Decimal(100))]),
        ("Cotton 95% / Spandex 5%", [("Cotton", Decimal(95)), ("Spandex", Decimal(5))]),
        (
            "12.5% Elastane, 87.5% Nylon",
            [("Elastane", Decimal("12.5")), ("Nylon", Decimal("87.5"))],
        ),
        (
            "50% Cotton; Viscose 30% + 15% Linen and Silk 5%",
            [
                ("Cotton", Decimal(50)),
                ("Viscose", Decimal(30)),
                ("Linen", Decimal(15)),
                ("Silk", Decimal(5)),
            ],
        ),
    ],
)
def test_happy(text: str, expected: list[tuple[str, Decimal]]) -> None:
    assert _pairs(text) == expected


def test_normalized_shape() -> None:
    fibres = parse_composition("60% Cotton, 40%   Recycled  Polyester")
    assert normalized(fibres) == [
        {"fibre": "cotton", "percent": 60},
        {"fibre": "recycled polyester", "percent": 40},
    ]
    half = normalized(parse_composition("12.5% Elastane, 87.5% Nylon"))
    assert half == [{"fibre": "elastane", "percent": 12.5}, {"fibre": "nylon", "percent": 87.5}]
    assert isinstance(half[0]["percent"], float)
    assert isinstance(normalized(parse_composition("100% cotton"))[0]["percent"], int)


# ---- refusals ----


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("59% a, 40% b", "sum"),
        ("101% a", "percent"),
        ("60% a, 40% A", "duplicate"),
        ("0% a, 100% b", "percent"),
        ("60% , 40% b", "blank"),
        ("60.25% a, 39.75% b", "format"),
        ("NaN% a", "format"),
        ("inf% a", "format"),
        ("-10% a", "format"),
        ("", "blank"),
        ("   ", "blank"),
        (", ".join(f"{p}% f{i}" for i, p in enumerate([10] * 9 + [5, 5])), "count"),
        ("100% " + "x" * 61, "format"),
        ("60 Cotton", "format"),
    ],
)
def test_refusals(text: str, reason: str) -> None:
    with pytest.raises(CompositionError) as caught:
        parse_composition(text)
    assert caught.value.reason == reason


def test_sum_refusal_named() -> None:
    with pytest.raises(CompositionError) as caught:
        parse_composition("59% a, 40% b")
    assert caught.value.reason == "sum"


# ---- Unicode ----


def test_unicode_fibres_kept() -> None:
    assert _pairs("60% Coton égyptien, 40% 綿") == [
        ("Coton égyptien", Decimal(60)),
        ("綿", Decimal(40)),
    ]


def test_nfc_normalized() -> None:
    decomposed = "Coté"  # "Coté" with a combining accent
    (fibre,) = parse_composition(f"100% {decomposed}")
    assert fibre.fibre == "Coté"


def test_nul_refused() -> None:
    with pytest.raises(CompositionError) as caught:
        parse_composition("100% cot\x00ton")
    assert caught.value.reason == "format"


def test_fullwidth_percent_refused() -> None:
    with pytest.raises(CompositionError) as caught:
        parse_composition("100\uff05 cotton")
    assert caught.value.reason == "format"


def test_sixty_character_fibre_passes() -> None:
    (fibre,) = parse_composition("100% " + "x" * 60)
    assert len(fibre.fibre) == 60


# ---- properties ----

_fibre_names = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
    min_size=1,
    max_size=10,
    unique=True,
)


@st.composite
def _compositions(draw: st.DrawFn) -> list[tuple[str, int]]:
    names = draw(_fibre_names)
    n = len(names)
    # n positive integers summing to 100
    cuts = sorted(draw(st.lists(st.integers(1, 99), min_size=n - 1, max_size=n - 1, unique=True)))
    bounds = [0, *cuts, 100]
    percents = [bounds[i + 1] - bounds[i] for i in range(n)]
    return list(zip(names, percents, strict=True))


@given(_compositions())
def test_round_trip_both_forms(parts: list[tuple[str, int]]) -> None:
    front = ", ".join(f"{p}% {name}" for name, p in parts)
    back = ", ".join(f"{name} {p}%" for name, p in parts)
    want = [Fibre(fibre=name, percent=Decimal(p)) for name, p in parts]
    assert list(parse_composition(front)) == want
    assert list(parse_composition(back)) == want


@given(st.lists(st.integers(1, 100), min_size=1, max_size=10).filter(lambda ps: sum(ps) != 100))
def test_not_summing_to_100_raises_sum(percents: list[int]) -> None:
    text = ", ".join(f"{p}% fibre{chr(97 + i)}" for i, p in enumerate(percents))
    with pytest.raises(CompositionError) as caught:
        parse_composition(text)
    assert caught.value.reason == "sum"
