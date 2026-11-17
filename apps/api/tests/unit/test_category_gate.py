"""SI14 T8: which categories may be offered (lamps only while the switch is on)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.config import Settings
from app.repositories.templates import CurrentCategory
from app.services.category_gate import (
    LAMPS,
    MAX_KEY_ATTRIBUTES,
    MAX_OFFERED,
    NOT_LISTED,
    NOT_LISTED_NAME,
    UNCATEGORIZED,
    allowed_categories,
    category_enabled,
    category_offers,
    classify_options,
    offer_digest_slugs,
)


@dataclass
class _Templates:
    rows: list[CurrentCategory]

    async def current_categories(self) -> list[CurrentCategory]:
        return self.rows


@dataclass
class _Uow:
    templates: _Templates


def _settings(lamps: bool) -> Settings:
    return Settings(_env_file=None, app_env="local", category_lamps_enabled=lamps)


def _uow(*names: str) -> _Uow:
    return _Uow(_Templates([CurrentCategory(n, 2, ("fabric", "Bad-Key")) for n in names]))


def test_lamps_follow_the_switch() -> None:
    assert not category_enabled(LAMPS, _settings(False))
    assert category_enabled(LAMPS, _settings(True))
    assert category_enabled("t_shirts", _settings(False))


def test_offers_filter_lamps_and_bad_keys() -> None:
    uow = _uow("ethnic_wear_kurta_sets", LAMPS, UNCATEGORIZED)
    assert asyncio.run(allowed_categories(uow, _settings(False))) == ["ethnic_wear_kurta_sets"]
    offers = asyncio.run(category_offers(uow, _settings(True)))
    assert [o.category for o in offers] == ["ethnic_wear_kurta_sets", LAMPS]
    assert offers[0].key_attributes == ("fabric",) and offers[0].template_version == 2


def test_offers_capped() -> None:
    uow = _uow(*(f"cat_{n:03d}" for n in range(MAX_OFFERED + 5)))
    # the last request slot is the not_listed option
    assert len(asyncio.run(allowed_categories(uow, _settings(False)))) == MAX_OFFERED - 1
    offers = asyncio.run(category_offers(uow, _settings(False)))
    assert len(classify_options(offers)) == MAX_OFFERED


def test_offers_carry_names_and_distinguishing_keys() -> None:
    row = CurrentCategory(
        "tshirts",
        3,
        ("color", "fabric_type"),
        ("color", "fabric_type", "fit", "neckline", "country_of_origin", "manufacturer", "Bad"),
    )
    [offer] = asyncio.run(category_offers(_Uow(_Templates([row])), _settings(False)))
    assert offer.name == "T-shirts"
    assert offer.key_attributes == ("fit", "neckline")


def test_key_attributes_capped() -> None:
    keys = tuple(f"key_{n:02d}" for n in range(MAX_KEY_ATTRIBUTES + 10))
    row = CurrentCategory("jeans", 1, (), keys)
    [offer] = asyncio.run(category_offers(_Uow(_Templates([row])), _settings(False)))
    assert offer.key_attributes == keys[:MAX_KEY_ATTRIBUTES]
    assert offer.name == "Jeans"


def test_classify_options_end_with_not_listed() -> None:
    uow = _uow("ethnic_wear_kurta_sets", "tshirts")
    offers = asyncio.run(category_offers(uow, _settings(False)))
    options = classify_options(offers)
    assert [o.category for o in options] == ["ethnic_wear_kurta_sets", "tshirts", NOT_LISTED]
    assert [o.name for o in options] == ["Kurta sets", "T-shirts", NOT_LISTED_NAME]
    assert options[-1].key_attributes == [] and options[-1].template_version is None
    assert offer_digest_slugs(offers) == ["ethnic_wear_kurta_sets", "tshirts", NOT_LISTED]
