"""SI1 T12: `plan_patch`, the pure change set of a product PATCH (D1b, CP13, CP14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef
from app.services.product_edit import plan_patch
from mkos_schemas.codes import ErrorCode

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
COLOR = AttributeDef("color", "attributes", ("colour", "style", "standalone"), ())
FIT = AttributeDef("fit", "attributes", ("style", "variant", "standalone"), ())
DEFS = (COLOR, FIT)
SELLER = {"source": "seller", "captured_via": "api", "updated_at": "2026-11-01T00:00:00Z"}


def _row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "title": "Tee",
        "category": "t_shirts",
        "style_number": None,
        "mrp": None,
        "selling_price": None,
        "manufacturer_party_id": None,
        "parent_product_id": None,
        "variation_theme": None,
        "attributes": {"color": {"value": "red", **SELLER}},
        "provenance": {},
    }
    base.update(over)
    return SimpleNamespace(**base)


def _plan(patch: dict[str, Any], row: Any = None, **kw: Any) -> Any:
    kw.setdefault("level", "standalone")
    kw.setdefault("has_listing", False)
    return plan_patch(row or _row(), kw.pop("style", None), patch, DEFS, now=NOW, **kw)


def _refused(patch: dict[str, Any], **kw: Any) -> InputError:
    with pytest.raises(InputError) as caught:
        _plan(patch, **kw)
    return caught.value


def test_empty_patch_is_empty() -> None:
    assert _plan({}).empty


def test_same_values_are_empty() -> None:
    row = _row(mrp=Decimal("999.00"), style_number="ST-1")
    change = _plan(
        {"title": "Tee", "mrp": Decimal("999"), "style_number": "ST-1", "attributes": {}},
        row=row,
    )
    assert change.empty


def test_same_attribute_value_is_empty() -> None:
    assert _plan({"attributes": {"color": "red"}}).empty


def test_title_change_and_clear() -> None:
    change = _plan({"title": "New", "attributes": {"color": None}})
    assert change.typed_set == {"title": "New"}
    assert change.attrs_clear == ("color",)


def test_clear_of_absent_value_is_empty() -> None:
    assert _plan({"mrp": None, "style_number": None, "size_chart": None}).empty


def test_clear_mrp_names_the_key() -> None:
    assert _plan({"mrp": None}, row=_row(mrp=Decimal(1))).typed_clear == ("mrp",)


def test_null_title_refused() -> None:
    assert _refused({"title": None}).field == "title"


def test_style_number_on_variant_refused() -> None:
    error = _refused({"style_number": "X"}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "style_number")


def test_size_chart_on_variant_refused() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 90}]}
    error = _refused({"size_chart": chart}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "size_chart")


def test_attribute_outside_level_refused_on_variant() -> None:
    error = _refused({"attributes": {"color": "blue"}}, level="variant")
    assert (error.code, error.field) == (ErrorCode.FACT_LEVEL_INVALID, "attributes.color")


def test_unknown_attribute_clear_refused() -> None:
    assert _refused({"attributes": {"shade": None}}).code is ErrorCode.UNKNOWN_FACT_KEY


def test_category_with_listing_refused() -> None:
    assert _refused({"category": "kurtas"}, has_listing=True).field == "category"
    assert _plan({"category": "kurtas"}).typed_set == {"category": "kurtas"}


def test_price_above_own_mrp_refused() -> None:
    error = _refused({"mrp": Decimal("10"), "selling_price": Decimal("10.01")})
    assert error.field == "selling_price"


def test_mrp_lowered_under_own_price_named_mrp() -> None:
    row = _row(selling_price=Decimal("500"), mrp=Decimal("600"))
    assert _refused({"mrp": Decimal("499")}, row=row).field == "mrp"


def test_variant_price_above_style_mrp_refused() -> None:
    style = _row(mrp=Decimal("999"))
    row = _row(parent_product_id=uuid.uuid4())
    error = _refused({"selling_price": Decimal("1000")}, row=row, level="variant", style=style)
    assert error.field == "selling_price"


def test_style_mrp_under_child_price_refused() -> None:
    row = _row(variation_theme="size")
    error = _refused(
        {"mrp": Decimal("899")},
        row=row,
        level="style",
        children=[(Decimal("900"), None)],
    )
    assert error.field == "mrp"
    # a child with its own MRP is judged against that, not the style's
    ok = _plan(
        {"mrp": Decimal("899")}, row=row, level="style", children=[(Decimal("900"), Decimal("950"))]
    )
    assert ok.typed_set == {"mrp": Decimal("899")}


def test_manufacturer_needs_party_id() -> None:
    party = uuid.uuid4()
    change = _plan({"manufacturer": object()}, party_id=party)
    assert change.typed_set == {"manufacturer_party_id": party}
    row = _row(manufacturer_party_id=party)
    assert _plan({"manufacturer": object()}, row=row, party_id=party).empty
    assert _plan({"manufacturer": None}, row=row).typed_clear == ("manufacturer",)


def test_size_chart_same_table_is_empty() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 90}]}
    row = _row(attributes={"size_measurements": {"normalized": chart, **SELLER}})
    assert _plan({"size_chart": chart}, row=row).empty
    changed = {**chart, "basis": "garment"}
    assert set(_plan({"size_chart": changed}, row=row).attrs_set) == {"size_measurements"}
