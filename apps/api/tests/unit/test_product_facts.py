"""S12 T4 (AC4; DATABASE.md:1082-1115, :1197; PLAN D3, D4): flat seller attributes become seller
`Fact` envelopes under their canonical dictionary keys; anything the dictionary does not allow on
a standalone product is refused with the field named, never the value."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.core.errors import InputError
from app.repositories.attribute_definitions import AttributeDef
from app.services.product_facts import build_attributes, seller_provenance
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode

NOW = datetime(2026, 10, 6, 9, 30, 15, 123456, tzinfo=UTC)
STAMP = "2026-10-06T09:30:15.123456Z"
COLOR = AttributeDef("color", "attributes", ("colour", "standalone"), ())
FABRIC = AttributeDef("fabric_type", "attributes", ("style", "standalone"), ("material",))
TITLE = AttributeDef("title", "column", ("style", "standalone"), ())
SECONDARY = AttributeDef("secondary_color", "attributes", ("colour",), ())
DEFS = (COLOR, FABRIC, TITLE, SECONDARY)


def _fact(value: object) -> dict[str, object]:
    return {"value": value, "source": "seller", "captured_via": "api", "updated_at": STAMP}


def _refused(inputs: dict[str, object], defs: tuple[AttributeDef, ...] = DEFS) -> InputError:
    with pytest.raises(InputError) as caught:
        build_attributes(inputs, defs, now=NOW)  # type: ignore[arg-type]
    return caught.value


def test_site_body_color_and_material() -> None:
    # spec L11317-11322: {"attributes": {"color": "navy blue", "material": "organic cotton"}}
    built = build_attributes({"color": "navy blue", "material": "organic cotton"}, DEFS, now=NOW)
    assert built == {"color": _fact("navy blue"), "fabric_type": _fact("organic cotton")}


def test_key_beats_alias() -> None:
    # a dictionary row whose key is another row's alias: the exact key wins
    material = AttributeDef("material", "attributes", ("standalone",), ())
    built = build_attributes({"material": "silk"}, (FABRIC, material), now=NOW)
    assert built == {"material": _fact("silk")}


def test_unknown_key_unknown_fact_key() -> None:
    error = _refused({"color": "red", "shade": "dark"})
    assert error.code is ErrorCode.UNKNOWN_FACT_KEY
    assert error.field == "attributes.shade"


def test_column_key_title_validation_failed() -> None:
    error = _refused({"title": "A title"})
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.field == "attributes.title"


def test_non_standalone_key_validation_failed() -> None:
    error = _refused({"secondary_color": "white"})
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.field == "attributes.secondary_color"


def test_alias_and_key_same_canonical_validation_failed() -> None:
    error = _refused({"material": "silk", "fabric_type": "cotton"})
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.field == "attributes.fabric_type"


def test_error_never_carries_the_value() -> None:
    error = _refused({"shade": "CANARY-value-s12"})
    assert "CANARY" not in str(error)
    assert all("CANARY" not in str(a) for a in error.args)


def test_empty_inputs() -> None:
    assert build_attributes({}, DEFS, now=NOW) == {}
    assert build_attributes({}, (), now=NOW) == {}


def test_unicode_value_kept() -> None:
    built = build_attributes({"color": "नीला 🌊"}, DEFS, now=NOW)
    assert built["color"]["value"] == "नीला 🌊"


@pytest.mark.parametrize("value", [True, False, 0, 7, 2.5, -1.25])
def test_scalar_types_kept(value: object) -> None:
    built = build_attributes({"color": value}, DEFS, now=NOW)  # type: ignore[dict-item]
    assert built["color"]["value"] == value
    assert type(built["color"]["value"]) is type(value)


def test_fact_has_exactly_four_keys() -> None:
    built = build_attributes({"color": "red"}, DEFS, now=NOW)
    assert set(built["color"]) == {"value", "source", "captured_via", "updated_at"}


def test_updated_at_is_utc_iso() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    built = build_attributes({"color": "red"}, DEFS, now=NOW.astimezone(ist))
    assert built["color"]["updated_at"] == STAMP
    with pytest.raises(ValueError, match="aware"):
        build_attributes({"color": "red"}, DEFS, now=NOW.replace(tzinfo=None))


def test_provenance_has_title_and_category_without_value() -> None:
    provenance = seller_provenance(now=NOW)
    entry = {"source": "seller", "captured_via": "api", "updated_at": STAMP}
    assert provenance == {"title": entry, "category": entry}
    assert all("value" not in fact for fact in provenance.values())


_VALUES = st.one_of(
    st.text(max_size=40),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)


@given(st.dictionaries(st.sampled_from(["color", "fabric_type"]), _VALUES))
def test_built_facts_shape(inputs: dict[str, object]) -> None:
    built = build_attributes(inputs, DEFS, now=NOW)  # type: ignore[arg-type]
    assert set(built) == set(inputs)
    for key, fact in built.items():
        assert set(fact) == {"value", "source", "captured_via", "updated_at"}
        assert fact["source"] == "seller"
        assert fact["value"] == inputs[key]


# ---- SI1 T7: levels, composition, size chart ----

from app.services.product_facts import (  # noqa: E402
    FACT_MAP_MAX_BYTES,
    SIZE_CHART_LEVELS,
    jsonb_text_bytes,
    owned_at,
    seller_column_fact,
    size_chart_fact,
)

FIBRES = AttributeDef(
    "fiber_composition",
    "attributes",
    ("style", "colour", "variant", "standalone"),
    ("fabric_composition",),
    "composition",
)
FABRIC_SI1 = AttributeDef(
    "fabric_type", "attributes", ("style", "colour", "variant", "standalone"), ("material",)
)
CHART_URL = AttributeDef("size_chart_url", "attributes", ("style", "standalone"), ())
MEASURES = AttributeDef(
    "size_measurements", "attributes", ("style", "colour", "variant", "standalone"), (), "object"
)
SI1_DEFS = (FIBRES, FABRIC_SI1, CHART_URL, MEASURES, COLOR)


def test_fabric_composition_normalized() -> None:
    built = build_attributes({"fabric_composition": "60% Cotton, 40% Polyester"}, SI1_DEFS, now=NOW)
    assert built == {
        "fiber_composition": {
            "value": "60% Cotton, 40% Polyester",
            "normalized": [
                {"fibre": "cotton", "percent": 60},
                {"fibre": "polyester", "percent": 40},
            ],
            "source": "seller",
            "captured_via": "api",
            "updated_at": STAMP,
        }
    }


@pytest.mark.parametrize(
    "text", ["59% a, 40% b", "101% a", "60% a, 40% A", "0% a, 100% b", "60% , 40% b"]
)
@pytest.mark.parametrize("name", ["fabric_composition", "fiber_composition"])
def test_bad_composition_422_at_canonical_key(text: str, name: str) -> None:
    error = _refused({name: text}, SI1_DEFS)
    assert (error.code, error.field) == (
        ErrorCode.VALIDATION_FAILED,
        "attributes.fiber_composition",
    )


def test_composition_not_text_refused() -> None:
    error = _refused({"fiber_composition": 100}, SI1_DEFS)
    assert error.field == "attributes.fiber_composition"


def test_material_still_maps_to_fabric_type() -> None:
    assert set(build_attributes({"material": "linen"}, SI1_DEFS, now=NOW)) == {"fabric_type"}


def test_alias_and_canonical_together_refused() -> None:
    error = _refused(
        {"fabric_composition": "100% cotton", "fiber_composition": "100% cotton"}, SI1_DEFS
    )
    assert error.code is ErrorCode.VALIDATION_FAILED


def test_size_chart_url_level() -> None:
    url = {"size_chart_url": "https://cdn.example.com/chart.png"}
    with pytest.raises(InputError) as caught:
        build_attributes(url, SI1_DEFS, now=NOW, level="variant")
    assert caught.value.code is ErrorCode.FACT_LEVEL_INVALID
    assert set(build_attributes(url, SI1_DEFS, now=NOW)) == {"size_chart_url"}
    assert set(build_attributes(url, SI1_DEFS, now=NOW, level="style")) == {"size_chart_url"}


@pytest.mark.parametrize(
    "url",
    ["http://cdn.example.com/c.png", "https://", "ftp://x/y", "https://x/" + "a" * 2000, 5],
)
def test_size_chart_url_must_be_https(url: object) -> None:
    error = _refused({"size_chart_url": url}, SI1_DEFS)
    assert error.field == "attributes.size_chart_url"


def test_standalone_level_outside_is_validation_failed() -> None:
    error = _refused({"secondary_color": "red"})
    assert error.code is ErrorCode.VALIDATION_FAILED


def test_size_chart_levels_explicit() -> None:
    assert owned_at(SI1_DEFS, "size_measurements", "variant")
    assert "variant" not in SIZE_CHART_LEVELS
    assert {"style", "standalone"} == SIZE_CHART_LEVELS
    assert not owned_at(SI1_DEFS, "no_such_key", "style")


def test_size_chart_fact_has_no_value() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 100}]}
    fact = size_chart_fact(chart, now=NOW)
    assert fact.get("value") is None
    assert fact["normalized"] == chart
    assert fact["source"] == "seller"


def test_size_chart_fact_bad_decimals_422() -> None:
    chart = {"unit": "cm", "basis": "body", "rows": [{"size": "M", "chest": 100.25}]}
    with pytest.raises(InputError) as caught:
        size_chart_fact(chart, now=NOW)
    assert caught.value.field == "size_chart"


def test_forty_row_chart_fits_the_map_cap() -> None:
    rows = [
        {
            "size": f"SIZE-{i:02d}-XXXXXXXX",
            **dict.fromkeys(
                ("chest", "waist", "hip", "length", "shoulder", "sleeve", "inseam"), 499.9
            ),
        }
        for i in range(40)
    ]
    fact = size_chart_fact({"unit": "cm", "basis": "garment", "rows": rows}, now=NOW)
    assert jsonb_text_bytes({"size_measurements": fact}) < FACT_MAP_MAX_BYTES


def test_seller_column_fact_has_no_value() -> None:
    assert seller_column_fact(now=NOW) == {
        "source": "seller",
        "captured_via": "api",
        "updated_at": STAMP,
    }
