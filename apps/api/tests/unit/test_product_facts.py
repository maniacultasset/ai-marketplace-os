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
