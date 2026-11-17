"""Feedback 17 Nov: each category's size step (`services/size_profiles.py`)."""

from __future__ import annotations

import pytest
from app.services import size_profiles

LETTERS = ("XS", "S", "M", "L", "XL", "XXL", "3XL")
WAIST = tuple(str(n) for n in range(26, 46, 2))


def test_launch_apparel_keeps_the_old_web_defaults() -> None:
    for slug in ("tshirts", "ethnic_wear_kurta_sets"):
        got = size_profiles.profile_for(slug)
        assert got == size_profiles.SizeProfile(
            "alpha", LETTERS, ("chest", "waist", "hip", "length")
        )


@pytest.mark.parametrize(
    ("slug", "system", "sizes", "measures"),
    [
        ("jeans", "waist", WAIST, ("waist", "hip", "inseam", "length")),
        ("trousers", "waist", WAIST, ("waist", "hip", "inseam", "length")),
        ("track_pants", "alpha", LETTERS, ("waist", "hip", "inseam", "length")),
        ("shorts", "waist", WAIST[:8], ("waist", "hip", "length")),
        ("skirts", "alpha", LETTERS, ("waist", "hip", "length")),
    ],
)
def test_bottomwear_profiles(
    slug: str, system: str, sizes: tuple[str, ...], measures: tuple[str, ...]
) -> None:
    got = size_profiles.profile_for(slug)
    assert got is not None
    assert (got.size_system, got.sizes, got.measures) == (system, sizes, measures)
    assert "chest" not in got.measures


@pytest.mark.parametrize("slug", ["home_decor_lamps", "uncategorized", "nope", None, ""])
def test_no_size_step(slug: str | None) -> None:
    assert size_profiles.profile_for(slug) is None


def test_every_profile_size_is_in_its_amazon_template() -> None:
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    for seed in (*seed_templates(), *flipkart_seed_templates()):
        profile = size_profiles.profile_for(seed.template.category)
        if profile is None:
            continue
        allowed = seed.template.specs["size"].allowed_values or []
        assert set(profile.sizes) <= set(allowed), seed.template.category


@pytest.mark.parametrize(
    "raw",
    [
        {"size_system": "metric", "sizes": ["S"], "measures": ["waist"]},
        {"size_system": "alpha", "sizes": [], "measures": ["waist"]},
        {"size_system": "alpha", "sizes": ["S", "S"], "measures": ["waist"]},
        {"size_system": "alpha", "sizes": ["S"], "measures": ["thigh"]},
        {"size_system": "alpha", "sizes": ["S"], "measures": []},
    ],
)
def test_bad_profiles_refused(raw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        size_profiles.parse({"profiles": {"x": raw}})
