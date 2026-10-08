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
