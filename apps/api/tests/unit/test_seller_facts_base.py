"""SI2 T1 (AC6, AC10): the seller-facts types and pure helpers in `app/adapters/base.py`."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from app.adapters.base import (
    AdapterItem,
    AdapterListing,
    SellerFacts,
    composition_text,
    fact_paths,
    locator_ok,
    money,
    nearest_values,
)
from hypothesis import given
from hypothesis import strategies as st
from tests.adapters.contract import sample_listing

COTTON_POLY = [{"fiber": "cotton", "percent": 60}, {"fiber": "polyester", "percent": 40}]

# ---- composition ----


def test_composition_si1_parts_without_text() -> None:
    assert composition_text(None, COTTON_POLY) == "60% Cotton, 40% Polyester"


def test_composition_si1_text_wins_trimmed() -> None:
    assert (
        composition_text(" 60% Cotton, 40% Polyester ", COTTON_POLY) == "60% Cotton, 40% Polyester"
    )


def test_composition_fibre_spelling_and_decimals() -> None:
    parts = [{"fibre": "cotton", "percent": 60.5}, {"fibre": "silk", "percent": 39.5}]
    assert composition_text(None, parts) == "60.5% Cotton, 39.5% Silk"


def test_composition_raw_text_and_blank() -> None:
    assert composition_text("  100% Cotton ", None) == "100% Cotton"
    assert composition_text("   ", None) is None
    assert composition_text(None, None) is None


def test_composition_unicode_names() -> None:
    parts = [{"fiber": "coton\u00a0lycra", "percent": 50}, {"fiber": "रेशम", "percent": 50}]
    assert composition_text(None, parts) == "50% Coton Lycra, 50% रेशम"


@pytest.mark.parametrize("total", [(45, 45), (60, 50), (60, Decimal("39.99"))])
def test_composition_sum_over_100_raises(total: tuple[object, object]) -> None:
    parts = [{"fiber": "cotton", "percent": total[0]}, {"fiber": "silk", "percent": total[1]}]
    with pytest.raises(ValueError):
        composition_text(None, parts)
    with pytest.raises(ValueError):
        composition_text("100% Cotton", parts)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5, True, "abc", None, 0, 101])
def test_composition_bad_percent_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        composition_text(None, [{"fiber": "Cotton", "percent": bad}])


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [{"fiber": "  ", "percent": 100}],
        {"fiber": "cotton", "percent": 100},
        [{"name": "cotton", "percent": 100}],
        ["100% cotton"],
    ],
)
def test_composition_bad_shape_raises(parts: object) -> None:
    with pytest.raises(ValueError):
        composition_text(None, parts)


def test_composition_hundred_parts() -> None:
    parts = [{"fiber": f"f{i}", "percent": 1} for i in range(100)]
    text = composition_text(None, parts)
    assert text is not None and text.startswith("1% F0, 1% F1")


# ---- money ----


def test_money() -> None:
    assert money("1.10") == Decimal("1.10")
    assert str(money("499")) == "499.00"
    assert money(None) is None
    for bad in ("NaN", "Infinity", "abc", "-Infinity"):
        with pytest.raises(ValueError):
            money(bad)


@given(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("9999999999.99"), places=2))
def test_money_float_round_trip(q: Decimal) -> None:
    assert Decimal(repr(float(q))) == q


# ---- locator ----

LOCAL = "http://localhost:9000/marketplace-images-public/x.jpg"


@pytest.mark.parametrize("ref", ["https://images.kuinn.in/a.jpg", "HTTPS://images.kuinn.in/a.jpg"])
@pytest.mark.parametrize("preview", [True, False])
def test_locator_https(ref: str, preview: bool) -> None:
    assert locator_ok(ref, preview=preview, allow_http=False)


def test_locator_preview_tag_only_in_preview() -> None:
    tag = f"image:{uuid.uuid4()}"
    assert locator_ok(tag, preview=True, allow_http=False)
    assert not locator_ok(tag, preview=False, allow_http=False)
    assert not locator_ok(tag, preview=False, allow_http=True)


def test_locator_http_only_when_allowed() -> None:
    assert locator_ok(LOCAL, preview=False, allow_http=True)
    assert not locator_ok(LOCAL, preview=False, allow_http=False)
    assert not locator_ok(LOCAL, preview=True, allow_http=False)


@pytest.mark.parametrize(
    "ref",
    [
        "//host/x",
        "/x",
        "",
        "   ",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "image:abc",
        "image:",
        "https://",
        "https://images.kuinn.in/a\n.jpg",
        "https://images.kuinn.in/a b.jpg",
    ],
)
@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("allow_http", [True, False])
def test_locator_refused(ref: str, preview: bool, allow_http: bool) -> None:
    assert not locator_ok(ref, preview=preview, allow_http=allow_http)


# ---- nearest ----

COLOURS = ["Black", "Blue", "Mustard", "Maroon", "Navy Blue", "Peach", "White"]


def test_nearest_values() -> None:
    assert nearest_values("Mustrd", COLOURS)[0] == "Mustard"
    out = nearest_values("Neon Plaid", COLOURS)
    assert 0 < len(out) <= 5
    assert nearest_values("Neon Plaid", COLOURS) == out
    assert nearest_values("zzzzzzzz", COLOURS) == COLOURS[:5]


# ---- fact paths and the listing ----


def test_fact_paths_by_role() -> None:
    pid = uuid.uuid4()
    parent = fact_paths(AdapterItem(pid, "S", "parent"))
    child = fact_paths(AdapterItem(pid, "S-1", "child", {"size": "S"}))
    alone = fact_paths(AdapterItem(pid, "S", "standalone"))
    assert "facts.mrp" not in parent and "facts.manufacturer" in parent
    assert {"facts.mrp", "facts.selling_price"} <= set(child) <= set(alone)
    assert list(alone) == sorted(alone)


def test_fact_paths_cover_style_identity_for_every_role() -> None:
    """SI8 T1 (CP-D2): brand, gender and style number are style-owned and reported at
    `facts.<key>` for every role, a child included."""
    pid = uuid.uuid4()
    style = {"facts.brand_name", "facts.gender", "facts.style_number"}
    for unit in (
        AdapterItem(pid, "S", "parent"),
        AdapterItem(pid, "S-1", "child", {"size": "S"}),
        AdapterItem(pid, "S", "standalone"),
    ):
        assert style <= set(fact_paths(unit))


def test_unit_facts_read_only_and_repr_has_no_address() -> None:
    body = sample_listing()
    facts = SellerFacts(
        mrp=Decimal("999.00"),
        manufacturer_name="Test Mills Pvt Ltd",
        manufacturer_address="1 Mill Road, Surat",
    )
    item = AdapterListing(
        listing_id=uuid.uuid4(),
        seller_sku="SKU-1",
        idempotency_key="k",
        listing=body,
        unit_facts={body.product_id: facts},
    )
    with pytest.raises(TypeError):
        item.unit_facts[uuid.uuid4()] = facts  # type: ignore[index]
    assert "Mill Road" not in repr(facts)
    assert "Mill Road" not in repr(item)
    assert "Test Mills" in repr(facts)


def test_seller_facts_identifier_defaults_empty() -> None:
    """SI5 B1: an item carries no identifier and no exemption unless the seller gave one."""
    facts = SellerFacts()
    assert (facts.identifier_type, facts.identifier_value, facts.identifier_exempt) == (
        None,
        None,
        False,
    )
