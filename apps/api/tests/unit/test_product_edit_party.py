"""`PATCH /products/{id}` manufacturer: a corrected address of the matched party is saved, never
dropped (`product_edit._party_moved`); a brand default party is refused (decision 16)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError
from app.repositories.parties import PartyFields
from app.services.product_edit import BRAND_PARTY_MSG, _party_moved
from mkos_schemas.codes import ErrorCode

SENT = PartyFields(
    role="manufacturer",
    legal_name="Yash Knits",
    address_line1="4 Mill Street",
    city="Tiruppur",
    state_code="TN",
    postal_code="641604",
)


def _uow(stored: Any, defaults: dict[str, Any] | None = None) -> SimpleNamespace:
    async def find(_fields: PartyFields) -> Any:
        return stored

    async def default_parties() -> dict[str, Any]:
        return defaults or {"manufacturer": None}

    return SimpleNamespace(
        parties=SimpleNamespace(find=find),
        brand_profiles=SimpleNamespace(default_parties=default_parties),
    )


def _stored(**over: Any) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "address_line2": None,
        "city": "Tiruppur",
        "state_code": "TN",
        "gstin": None,
    }
    return SimpleNamespace(**{**base, **over})


async def test_new_party_is_not_moved() -> None:
    assert await _party_moved(_uow(None), SENT) is False


async def test_same_address_is_not_moved() -> None:
    assert await _party_moved(_uow(_stored()), SENT) is False


@pytest.mark.parametrize(
    "over",
    [
        {"state_code": "SK"},  # the prod repro: Sikkim -> Tamil Nadu was dropped
        {"city": "Coimbatore"},
        {"address_line2": "Unit 2"},  # cleared
        {"gstin": "33ABCDE1234F1Z5"},  # cleared
    ],
)
async def test_corrected_address_is_moved(over: dict[str, Any]) -> None:
    assert await _party_moved(_uow(_stored(**over)), SENT) is True


async def test_added_gstin_is_moved() -> None:
    sent = replace(SENT, gstin="33ABCDE1234F1Z5")
    assert await _party_moved(_uow(_stored()), sent) is True


async def test_brand_default_party_is_refused_at_the_changed_field() -> None:
    stored = _stored(state_code="SK")
    uow = _uow(stored, {"manufacturer": None, "marketer": stored.id})
    with pytest.raises(InputError) as caught:
        await _party_moved(uow, SENT)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert caught.value.field == "manufacturer.state_code"
    assert caught.value.detail == BRAND_PARTY_MSG


async def test_brand_default_party_unchanged_links() -> None:
    stored = _stored()
    assert await _party_moved(_uow(stored, {"manufacturer": stored.id}), SENT) is False
