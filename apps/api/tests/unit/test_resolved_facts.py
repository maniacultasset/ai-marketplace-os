"""S18 T1 (AC 8; D5; DATABASE.md:1189-1192): the resolved `ProductFacts`, its two projections and
its digest."""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.resolved_facts import (
    RULES_KEYS,
    CellFacts,
    Commercial,
    Compliance,
    FamilySummary,
    Identity,
    PartyFacts,
    ProductFacts,
    ResolvedFact,
    facts_digest,
    for_prompt,
    for_rules,
    is_unconfirmed_ai,
    recheck_digest,
    stamp_reread,
)
from hypothesis import given, settings
from hypothesis import strategies as st

DIGEST_VECTOR = "1ca510468346f70eb0781df5a52cf6847babc81eb32607d173875f7d85480e04"
PRODUCT = uuid.UUID("00000000-0000-4000-8000-000000000001")
PARTY = PartyFacts(
    legal_name="Acme Apparel Pvt Ltd",
    address="1 MG Road, Bengaluru",
    email="care@acme.example",
    phone="+91-80-0000-0000",
    gstin="29ABCDE1234F1Z5",
)


def facts(**overrides: Any) -> ProductFacts:
    base: dict[str, Any] = {
        "product_id": PRODUCT,
        "sku": "KURTA-001",
        "level": "standalone",
        "identity": Identity(title="Cotton Kurta Set", brand_name="Acme"),
        "commercial": Commercial(mrp="1499.00", selling_price="1.10", currency="INR"),
        "compliance": Compliance(
            country_of_origin="IN", manufacturer=PARTY, consumer_care=PartyFacts(email="x@y.in")
        ),
        "attributes": {
            "color": ResolvedFact(
                key="color", value="indigo", source="seller", updated_at="2026-10-05T00:00:00Z"
            )
        },
        "provenance": {
            "title": ResolvedFact(key="title", source="seller", updated_at="2026-10-05T00:00:00Z")
        },
        "dictionary_version": 3,
    }
    base.update(overrides)
    return ProductFacts(**base)


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_for_prompt_drops_party_contacts_and_gstin() -> None:
    shown = _text(for_prompt(facts()))
    for secret in ("29ABCDE1234F1Z5", "care@acme.example", "+91-80-0000-0000", "MG Road", "x@y.in"):
        assert secret not in shown
    assert "gstin" not in shown
    assert "consumer_care" not in for_prompt(facts())["compliance"]  # a nameless party is dropped


def test_for_prompt_keeps_legal_name() -> None:
    assert for_prompt(facts())["compliance"]["manufacturer"] == {"legal_name": PARTY.legal_name}


def test_for_prompt_has_no_evidence_or_dictionary_version() -> None:
    shown = for_prompt(facts())
    assert "provenance" not in shown and "dictionary_version" not in shown
    assert shown["attributes"] == {"color": "indigo"}


def test_digest_vector() -> None:
    assert facts_digest(facts()).hex() == DIGEST_VECTOR


def test_digest_ignores_key_order() -> None:
    a = facts(
        attributes={
            "color": ResolvedFact(key="color", value="indigo"),
            "fabric": ResolvedFact(key="fabric", value="cotton"),
        }
    )
    b = facts(
        attributes={
            "fabric": ResolvedFact(key="fabric", value="cotton"),
            "color": ResolvedFact(key="color", value="indigo"),
        }
    )
    assert facts_digest(a) == facts_digest(b)


def test_digest_decimal_as_string() -> None:
    a = facts(commercial=Commercial(mrp="1.10"))
    b = facts(commercial=Commercial(mrp="1.1"))
    assert facts_digest(a) != facts_digest(b)
    assert facts_digest(a) == facts_digest(facts(commercial=Commercial(mrp="1.10")))


def test_digest_ignores_updated_at() -> None:
    later = facts(
        attributes={
            "color": ResolvedFact(
                key="color", value="indigo", source="seller", updated_at="2027-01-01T00:00:00Z"
            )
        }
    )
    assert facts_digest(later) == facts_digest(facts())


def test_digest_changes_on_party_phone() -> None:
    other = facts(
        compliance=Compliance(
            country_of_origin="IN",
            manufacturer=PARTY.model_copy(update={"phone": "+91-80-1111-1111"}),
            consumer_care=PartyFacts(email="x@y.in"),
        )
    )
    assert facts_digest(other) != facts_digest(facts())
    assert for_prompt(other) == for_prompt(facts())  # the prompt cannot see the change


def test_digest_changes_on_product_weight() -> None:
    assert facts_digest(facts(product_weight_g=450)) != facts_digest(facts())


def test_digest_covers_dictionary_version() -> None:
    assert facts_digest(facts(dictionary_version=4)) != facts_digest(facts())


def test_for_rules_key_set_exact() -> None:
    assert set(for_rules(facts())) == set(RULES_KEYS)
    assert set(RULES_KEYS) == {
        "product_id",
        "sku",
        "level",
        "style_product_id",
        "colour_key",
        "size_key",
        "identity",
        "classification",
        "commercial",
        "identifier",
        "package",
        "product_weight_g",
        "compliance",
        "attributes",
        "family",
    }


def test_for_rules_drops_party_contacts_and_gstin() -> None:
    shown = _text(for_rules(facts()))
    for secret in ("29ABCDE1234F1Z5", "care@acme.example", "+91-80-0000-0000", "MG Road", "x@y.in"):
        assert secret not in shown
    assert for_rules(facts())["compliance"]["manufacturer"] == {"legal_name": PARTY.legal_name}
    assert "provenance" not in for_rules(facts())


def test_family_only_available_cells() -> None:
    family = FamilySummary(
        theme="size", size_run=["S", "M"], cells=[CellFacts(size_key="S"), CellFacts(size_key="M")]
    )
    shown = for_prompt(facts(level="style", family=family))
    assert [c["size_key"] for c in shown["family"]["cells"]] == ["S", "M"]


def test_for_prompt_unicode_and_blank_fields() -> None:
    shown = for_prompt(facts(identity=Identity(title="सूती कुर्ता सेट", brand_name="  ")))
    assert shown["identity"] == {"title": "सूती कुर्ता सेट"}
    assert "सूती" in json.dumps(shown, ensure_ascii=False)


_values = st.one_of(st.none(), st.text(max_size=8), st.integers(-5, 5), st.booleans())


@settings(max_examples=60, deadline=None)
@given(
    attrs=st.dictionaries(st.from_regex(r"[a-z]{2,6}", fullmatch=True), _values, max_size=6),
    sku=st.text(min_size=1, max_size=12),
)
def test_digest_deterministic(attrs: dict[str, Any], sku: str) -> None:
    one = facts(sku=sku, attributes={k: ResolvedFact(key=k, value=v) for k, v in attrs.items()})
    reordered = facts(
        sku=sku,
        attributes={k: ResolvedFact(key=k, value=v) for k, v in reversed(list(attrs.items()))},
    )
    assert facts_digest(one) == facts_digest(one)
    assert facts_digest(one) == facts_digest(reordered)


# ---- SI1 T9: structured party fields stay out of the digest (CP2) ----

STRUCTURED = {
    "address_line1": "1 MG Road",
    "address_line2": None,
    "city": "Bengaluru",
    "state_code": "KA",
    "postal_code": "560001",
    "country_code": "IN",
}


def test_structured_party_fields_keep_the_golden_digest() -> None:
    """DIGEST_VECTOR was computed before SI1 for this fixture; filling the structured fields
    must not move it, or every approval made before the deploy would fail its facts pin."""
    party = PARTY.model_copy(update=STRUCTURED)
    with_parts = facts(
        compliance=Compliance(
            country_of_origin="IN", manufacturer=party, consumer_care=PartyFacts(email="x@y.in")
        )
    )
    assert facts_digest(with_parts).hex() == DIGEST_VECTOR


def test_for_prompt_has_no_structured_address() -> None:
    party = PARTY.model_copy(update=STRUCTURED)
    body = for_prompt(facts(compliance=Compliance(manufacturer=party)))
    text = _text(body)
    for value in ("Bengaluru", "560001", "1 MG Road"):
        assert value not in text


def test_digest_changes_when_address_changes() -> None:
    moved = PARTY.model_copy(update={"address": "2 MG Road, Bengaluru"})
    assert facts_digest(facts(compliance=Compliance(manufacturer=moved))) != facts_digest(
        facts(compliance=Compliance(manufacturer=PARTY))
    )


# ---- SI3 T5: unconfirmed photo facts never reach the rules; confirmation stays out of the digest ----

AI_VECTOR = "97b8407a32b5b460d4798547e876d9decf1e12b7e5ea49336f60cb4bf89234a4"  # from main, pre-SI3
AT = "2026-11-07T10:00:00Z"
IMG = "image:6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11"
SELLER_COLOUR = ResolvedFact(
    key="color", value="indigo", source="seller", updated_at="2026-10-05T00:00:00Z"
)


def ai(key: str = "neckline", value: str = "v-neck", **extra: Any) -> ResolvedFact:
    base: dict[str, Any] = {
        "key": key,
        "value": value,
        "normalized": value,
        "source": "ai",
        "captured_via": "agent",
        "confidence": 0.95,
        "evidence_ref": IMG,
        "agent_run_id": "7a2b9c3d-0000-4000-8000-000000000001",
        "updated_at": "2026-11-07T00:00:00Z",
    }
    base.update(extra)
    return ResolvedFact(**base)


def with_attrs(**attrs: ResolvedFact) -> ProductFacts:
    return facts(attributes={"color": SELLER_COLOUR, **attrs})


def test_is_unconfirmed_ai() -> None:
    assert is_unconfirmed_ai(ai())
    assert not is_unconfirmed_ai(ai(confirmed_by="u", confirmed_at=AT))
    assert not is_unconfirmed_ai(SELLER_COLOUR)


def test_for_rules_drops_unconfirmed_ai_only() -> None:
    attrs = {
        "neckline": ai(),
        "fit": ai("fit", "slim fit", confirmed_by="u", confirmed_at=AT),
        "pattern": ResolvedFact(key="pattern", value="solid", source="document"),
        "occasion": ResolvedFact(key="occasion", value="festive", source="derived", confidence=0.8),
        "embroidery": ai("embroidery", "zari work", inherited_from="style"),
    }
    rules = for_rules(with_attrs(**attrs))["attributes"]
    assert "neckline" not in rules and "embroidery" not in rules
    assert rules["fit"] == "slim fit" and rules["pattern"] == "solid"
    assert rules["occasion"] == "festive" and rules["color"] == "indigo"
    prompt = for_prompt(with_attrs(**attrs))["attributes"]
    assert {"neckline", "embroidery", "fit", "pattern", "occasion", "color"} <= set(prompt)


def test_digest_stability_constants() -> None:
    assert facts_digest(facts()).hex() == DIGEST_VECTOR
    assert facts_digest(with_attrs(neckline=ai())).hex() == AI_VECTOR


def test_confirming_leaves_the_digest() -> None:
    base = facts_digest(with_attrs(neckline=ai()))
    assert facts_digest(with_attrs(neckline=ai(confirmed_by="u", confirmed_at=AT))) == base
    assert facts_digest(with_attrs(neckline=ai(value="round neck"))) != base
    assert facts_digest(with_attrs(neckline=ai(confidence=0.9))) != base
    assert facts_digest(with_attrs(neckline=ai(evidence_ref="image:other"))) != base


def _stamp(pre: ProductFacts, reread: ProductFacts) -> bool:
    return stamp_reread(pre, reread)


def test_stamp_reread_true() -> None:
    pre = with_attrs()
    assert _stamp(pre, with_attrs(neckline=ai()))  # absent -> unconfirmed ai
    assert _stamp(with_attrs(neckline=ai()), with_attrs(neckline=ai(value="round neck")))
    assert _stamp(pre, pre)


def test_stamp_reread_false() -> None:
    seller = ResolvedFact(key="neckline", value="Round", source="seller")
    assert not _stamp(with_attrs(), with_attrs(neckline=seller))  # a new seller fact
    other_colour = SELLER_COLOUR.model_copy(update={"value": "red"})
    assert not _stamp(with_attrs(), facts(attributes={"color": other_colour}))
    assert not _stamp(with_attrs(neckline=ai()), with_attrs())  # a reject during the run
    assert not _stamp(
        with_attrs(neckline=ai()),
        with_attrs(neckline=ai(value="round neck", confirmed_by="u", confirmed_at=AT)),
    )
    assert not _stamp(with_attrs(), facts(attributes={"color": SELLER_COLOUR}, product_weight_g=9))
    moved = {"title": ResolvedFact(key="title", source="document")}
    assert not _stamp(with_attrs(), facts(attributes={"color": SELLER_COLOUR}, provenance=moved))
    pieces = ResolvedFact(key="number_of_pieces", value=3, source="seller", is_critical=True)
    assert not _stamp(with_attrs(), with_attrs(number_of_pieces=pieces))


def test_recheck_digest() -> None:
    pinned = b"p" * 32
    pre, reread = with_attrs(), with_attrs(neckline=ai())
    assert recheck_digest(pre, reread, pinned) == facts_digest(reread)
    assert recheck_digest(with_attrs(neckline=ai()), pre, pinned) == pinned


_AI_VALUES = st.sampled_from(["v-neck", "round neck", "boat neck"])
_KEYS = st.sampled_from(["neckline", "fit", "pattern", "garment_length", "occasion"])


@settings(max_examples=200, deadline=None)
@given(
    pre_ai=st.dictionaries(_KEYS, _AI_VALUES, max_size=4),
    changes=st.dictionaries(_KEYS, _AI_VALUES, max_size=5),
)
def test_stamp_reread_property(pre_ai: dict[str, str], changes: dict[str, str]) -> None:
    pre = with_attrs(**{k: ai(k, v) for k, v in pre_ai.items()})
    merged = {**pre_ai, **changes}
    reread = with_attrs(
        **{k: ai(k, v, updated_at="2026-11-08T00:00:00Z") for k, v in merged.items()}
    )
    assert stamp_reread(pre, reread)
    assert for_rules(reread) == for_rules(pre)
