"""DW2 T2: the PTD constants and the internal `ProductTypeSchema` / `AdapterListing` types."""

from __future__ import annotations

import dataclasses
import re
import uuid

import pytest
from app.adapters.amazon import constants as c
from app.adapters.base import AdapterListing, ProductTypeSchema
from tests.adapters.contract import sample_listing


def _schema(**over: object) -> ProductTypeSchema:
    fields: dict[str, object] = {
        "definition_id": uuid.uuid4(),
        "product_type": "KURTA",
        "product_type_version": "U1",
        "requirements": "LISTING_PRODUCT_ONLY",
        "enforced": True,
        "digest": b"\x01" * 32,
    }
    fields.update(over)
    return ProductTypeSchema(**fields)  # type: ignore[arg-type]


def test_schema_ref_valid() -> None:
    ref = _schema()
    assert ref.validator is None
    assert {f.name for f in dataclasses.fields(ref)} == {
        "definition_id",
        "product_type",
        "product_type_version",
        "requirements",
        "enforced",
        "digest",
        "validator",
    }


@pytest.mark.parametrize(
    "over",
    [
        {"digest": b"\x01" * 31},
        {"digest": b"\x01" * 33},
        {"digest": "x" * 32},
        {"product_type": ""},
        {"product_type": "  "},
        {"product_type_version": ""},
    ],
)
def test_schema_ref_refused(over: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _schema(**over)


def test_adapter_listing_default_has_no_schema() -> None:
    listing_id = uuid.uuid4()
    body = sample_listing()
    plain = AdapterListing(listing_id, "SKU-1", "key-1", body)
    assert plain.product_type_schema is None
    assert plain == AdapterListing(listing_id, "SKU-1", "key-1", body, product_type_schema=None)
    names = [f.name for f in dataclasses.fields(AdapterListing)]
    # SI2 appends its keyword-defaulted fields after the DW2 schema, SI9 its `live` flag
    assert names[-5:] == [
        "product_type_schema",
        "unit_facts",
        "parent_type_schema",
        "allow_http_images",
        "live",
    ]


def test_ptd_constants() -> None:
    assert c.PTD_PATH == "/definitions/2020-09-01/productTypes"
    assert c.PTD_REQUIREMENTS == c.REQUIREMENTS_SELLABLE == "LISTING"  # SI2 D3
    assert c.PTD_PARENT_REQUIREMENTS == c.REQUIREMENTS_PARENT == "LISTING_PRODUCT_ONLY"
    assert c.PTD_LOCALE == "en_IN"
    assert set(c.PTD_RATES) == set(c.PTD_OPERATIONS) == {"ptd_get"}
    # item-call operations and rates are untouched
    assert c.OPERATIONS == ("put", "patch", "get")
    assert dict(c.RATES) == {"put": 1, "patch": 1, "get": 1}
    pattern = re.compile(c.PRODUCT_TYPE_PATTERN)
    assert pattern.fullmatch("KURTA") and pattern.fullmatch("A" * 64)
    for bad in ("kurta", "A" * 65, "", "KURTA/..", "KÜRTA"):
        assert not pattern.fullmatch(bad)
