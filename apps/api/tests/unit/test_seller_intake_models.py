"""SI1 T5: the seller intake wire models (AC1, AC6, AC8, AC20)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.api.v1.schemas.brand_profile import BrandProfileIn, PartyIn
from app.api.v1.schemas.products import ProductCreate, ProductPatch, SizeChart
from pydantic import ValidationError

TENANT = uuid.uuid4()
PRODUCT = uuid.uuid4()


def _key() -> str:
    return f"{TENANT}/{PRODUCT}/images/{uuid.uuid4()}"


def _party(**over: Any) -> dict[str, Any]:
    party: dict[str, Any] = {
        "legal_name": "Acme Garments Pvt Ltd",
        "address_line1": "12 MG Road",
        "address_line2": "Floor 2",
        "city": "Bengaluru",
        "state_code": "KA",
        "postal_code": "560001",
        "gstin": "29ABCDE1234F1Z5",
        "email": "care@acme.example",
        "phone": "+91 80 1234 5678",
    }
    party.update(over)
    return party


def _create(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"sku": "TS-001", "title": "Tee", "category": "t_shirts"}
    body.update(over)
    return body


CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 101.5}]}


# ---- create ----


def test_create_every_field_valid() -> None:
    body = ProductCreate.model_validate(
        _create(
            id=str(PRODUCT),
            style_number="ST-9",
            mrp="1299.00",
            selling_price="999",
            manufacturer=_party(),
            size_chart=CHART,
            attributes={"fabric_composition": "60% Cotton, 40% Polyester"},
            image_uploads=[
                {"storage_key": _key(), "slot": "main"},
                {"storage_key": _key(), "slot": "packaging_label"},
            ],
        )
    )
    assert body.mrp == "1299.00" and body.selling_price == "999"
    assert body.manufacturer is not None and body.manufacturer.country_code == "IN"


def test_create_uploads_without_id_refused() -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(
            _create(image_uploads=[{"storage_key": _key(), "slot": "main"}])
        )


def test_create_id_without_uploads_refused() -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(_create(id=str(PRODUCT)))


def test_create_21_uploads_refused() -> None:
    uploads = [{"storage_key": _key(), "slot": "secondary"} for _ in range(21)]
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(_create(id=str(PRODUCT), image_uploads=uploads))


def test_create_bad_slot_refused() -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(
            _create(id=str(PRODUCT), image_uploads=[{"storage_key": _key(), "slot": "back"}])
        )


@pytest.mark.parametrize("style_number", ["S" * 65, "   ", "ST\x009"])
def test_create_bad_style_number(style_number: str) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(_create(style_number=style_number))


@pytest.mark.parametrize("money", ["0", "0.00", "-1", "1e3", "12.345", "01", 12.5, "NaN"])
def test_create_bad_money(money: Any) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(_create(mrp=money))


# ---- patch ----


def test_patch_empty_is_valid() -> None:
    assert ProductPatch.model_validate({}).model_fields_set == set()


def test_patch_sku_is_extra() -> None:
    with pytest.raises(ValidationError) as caught:
        ProductPatch.model_validate({"sku": "x"})
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_patch_null_is_set() -> None:
    patch = ProductPatch.model_validate({"mrp": None, "manufacturer": None})
    assert {"mrp", "manufacturer"} <= patch.model_fields_set
    assert patch.mrp is None


def test_patch_attribute_null_allowed() -> None:
    patch = ProductPatch.model_validate({"attributes": {"color": None}})
    assert patch.attributes == {"color": None}


@pytest.mark.parametrize("field", ["title", "category", "attributes"])
def test_patch_null_refused_on_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        ProductPatch.model_validate({field: None})


# ---- party ----


def test_party_happy_minimal() -> None:
    party = PartyIn.model_validate(_party(address_line2=None, gstin=None, email=None, phone=None))
    assert party.state_code == "KA"


@pytest.mark.parametrize(
    "over",
    [
        {"postal_code": "012345"},
        {"postal_code": "56001"},
        {"country_code": "US"},
        {"gstin": "29abcde1234f1z5"},
        {"email": "care @acme.example"},
        {"email": "a@" + "b" * 253},
        {"phone": "abc"},
        {"legal_name": "x" * 301},
        {"city": "Benga\x00luru"},
        {"address_line1": "  "},
        {"city": "c" * 101},
    ],
)
def test_party_checks_mirrored(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PartyIn.model_validate(_party(**over))


def test_party_state_unknown() -> None:
    with pytest.raises(ValidationError):
        PartyIn.model_validate(_party(state_code="XX"))
    with pytest.raises(ValidationError):
        PartyIn.model_validate(_party(state_code="OR"))


def test_brand_profile_absent_vs_null() -> None:
    assert BrandProfileIn.model_validate({}).model_fields_set == set()
    cleared = BrandProfileIn.model_validate({"default_manufacturer": None})
    assert "default_manufacturer" in cleared.model_fields_set


# ---- size chart ----


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"size": "M", "chest": float("nan")}],
        [{"size": "M", "chest": float("inf")}],
        [{"size": "M", "chest": 0}],
        [{"size": "M", "bust": 90}],
        [{"size": "M" * 21, "chest": 90}],
        [{"size": "M", "chest": 90}] * 41,
    ],
)
def test_size_chart_refused(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        SizeChart.model_validate({"unit": "cm", "basis": "garment", "rows": rows})
