"""S14a T12: the seed RuleSets resolve and run over the seed products (AC 10; D5, D12, R11577).

Each seed RuleSet's rows go through `resolve_ruleset` (as S14b's loader will) and then `evaluate`.
The goldens pin `attempts_used=0` (S18 sets it) and the `*` rule's suggestion (CODE-PLAN L5); the
duplicate BANNED_TERM items from the title rule and the `*` rule are accepted (L4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.services.rules_engine import RuleRow, evaluate, resolve_ruleset
from mkos_schemas.listing import CanonicalListing
from mkos_schemas.rules import RuleSet
from pydantic import TypeAdapter
from tests.rules_engine_builders import FIXTURES, LOADED_AT

HERE = Path(__file__).parent
LISTINGS = HERE / "fixtures"
GOLDEN = HERE / "golden"
SEED = FIXTURES / "rules.seed.json"
PRODUCTS = ("kurta_001", "kurta_002", "decor_001", "fsh_tee_001")
MARKETPLACES = ("amazon", "flipkart")
CATEGORY_PRODUCT = {
    "ethnic_wear_kurta_sets": "kurta_002",
    "home_decor_lamps": "decor_001",
    "tshirts": "fsh_tee_001",
}


def _seed() -> dict[tuple[str, str], RuleSet]:
    sets = TypeAdapter(list[RuleSet]).validate_json(SEED.read_bytes())
    return {(s.marketplace.value, s.category): s for s in sets}


def _listing(product: str, marketplace: str, **over: Any) -> CanonicalListing:
    data = json.loads((LISTINGS / f"{product}.{marketplace}.json").read_text(encoding="utf-8"))
    data.update(over)
    return CanonicalListing.model_validate(data)


def _resolved(marketplace: str, category: str) -> RuleSet:
    seed = _seed()[(marketplace, category)]
    rows = [RuleRow(rule, revision=index + 1) for index, rule in enumerate(seed.rules)]
    return resolve_ruleset(
        rows, marketplace=seed.marketplace, category=category, tenant_id=None, loaded_at=LOADED_AT
    )


def _run(item: CanonicalListing) -> Any:
    return evaluate(item, _resolved(item.marketplace.value, item.category))


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("product", PRODUCTS)
def test_rules_seed_resolves_and_runs(product: str, marketplace: str) -> None:
    item = _listing(product, marketplace)
    assert item.marketplace.value == marketplace
    rep = _run(item)
    assert rep.listing_id == item.listing_id
    assert rep.status in ("passed", "failed")


def test_rules_seed_every_ruleset_has_a_fixture_listing() -> None:
    covered = {
        (_listing(p, m).marketplace.value, _listing(p, m).category)
        for p in PRODUCTS
        for m in MARKETPLACES
    }
    assert covered == set(_seed())


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_flipkart_resolves_to_150(category: str) -> None:
    resolved = _resolved("flipkart", category)
    (title_max,) = [
        r for r in resolved.rules if r.rule_def.type == "max_length" and r.field == "content.title"
    ]
    assert title_max.rule_def.limit == 150  # type: ignore[union-attr]
    item = _listing(CATEGORY_PRODUCT[category], "flipkart")
    long_title = item.model_copy(
        update={"content": item.content.model_copy(update={"title": "x" * 151})}
    )
    codes = [(e.field, e.error_code.value) for e in _run(long_title).errors]
    assert ("content.title", "FIELD_TOO_LONG") in codes
    amazon = _resolved("amazon", category)
    (amazon_max,) = [
        r for r in amazon.rules if r.rule_def.type == "max_length" and r.field == "content.title"
    ]
    assert amazon_max.rule_def.limit == 200  # type: ignore[union-attr]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("term", ["best", "guaranteed", "Best", "GUARANTEED"])
def test_rules_seed_bare_best_flagged(marketplace: str, term: str) -> None:
    """R11577/R11577a: a bare "best" or "guaranteed" in a title fails on every seeded RuleSet."""
    for product in PRODUCTS:
        item = _listing(product, marketplace)
        title = f"{term} {item.content.title}"
        item = item.model_copy(update={"content": item.content.model_copy(update={"title": title})})
        rep = _run(item)
        assert rep.status == "failed"
        hits = [
            e
            for e in rep.errors
            if e.field == "content.title" and e.error_code.value == "BANNED_TERM"
        ]
        assert hits, (product, marketplace)
        assert any(term.casefold() in e.message.casefold() for e in hits)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_golden_kurta(marketplace: str) -> None:
    rep = _run(_listing("kurta_001", marketplace))
    golden = json.loads((GOLDEN / f"seed_kurta_001.{marketplace}.json").read_text(encoding="utf-8"))
    assert rep.model_dump(mode="json") == golden
    assert golden["status"] == "failed" and golden["attempts_used"] == 0
    assert any(e["suggestion"] for e in golden["errors"])


def test_rules_seed_golden_fsh_tee_001_amazon() -> None:
    rep = _run(_listing("fsh_tee_001", "amazon"))
    golden = json.loads((GOLDEN / "seed_fsh_tee_001.amazon.json").read_text(encoding="utf-8"))
    assert rep.model_dump(mode="json") == golden
    assert golden["status"] == "passed" and golden["errors"] == []


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_tshirts_missing_fabric_type_fails(marketplace: str) -> None:
    item = _listing("fsh_tee_001", marketplace)
    attributes = {k: v for k, v in item.attributes.items() if k != "fabric_type"}
    rep = _run(item.model_copy(update={"attributes": attributes}))
    assert rep.status == "failed"
    assert [(e.field, e.error_code.value) for e in rep.errors] == [
        ("attributes.fabric_type", "MISSING_ATTRIBUTE")
    ]


# ---- A2 (ai-deps PLAN C19): Amazon backend keywords fit in 250 UTF-8 bytes ----
#
# The engine joins the list with one space, normalises to NFC and counts UTF-8 bytes
# (`rules_engine/text.py` `measure`), so the joining spaces count toward the budget.


def _with_hidden(item: CanonicalListing, terms: list[str]) -> CanonicalListing:
    keywords = item.keywords.model_copy(update={"hidden_terms": terms})
    return item.model_copy(update={"keywords": keywords})


def _byte_errors(item: CanonicalListing) -> list[Any]:
    return [e for e in _run(item).errors if e.error_code.value == "BYTE_BUDGET_EXCEEDED"]


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_hidden_terms_250_bytes_pass(category: str) -> None:
    item = _listing(CATEGORY_PRODUCT[category], "amazon")
    assert _byte_errors(_with_hidden(item, ["a" * 250])) == []
    assert _byte_errors(_with_hidden(item, ["a" * 124, "b" * 125])) == []  # 124 + 1 + 125


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_hidden_terms_251_bytes_fail(category: str) -> None:
    item = _listing(CATEGORY_PRODUCT[category], "amazon")
    (error,) = _byte_errors(_with_hidden(item, ["a" * 251]))
    assert error.field == "keywords.hidden_terms"
    assert error.severity.value == "error" and error.rule_id is not None
    assert "250" in error.message
    assert _run(_with_hidden(item, ["a" * 251])).status == "failed"


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_hidden_terms_joining_space_counts(category: str) -> None:
    """Two terms of 125 bytes are 251 bytes once joined: the space tips them over."""
    item = _listing(CATEGORY_PRODUCT[category], "amazon")
    (error,) = _byte_errors(_with_hidden(item, ["a" * 125, "b" * 125]))
    assert error.field == "keywords.hidden_terms"


def test_rules_seed_hidden_terms_devanagari_counts_bytes_not_chars() -> None:
    """Devanagari letters are 3 UTF-8 bytes each: 83 letters (249 bytes) pass, 84 (252 bytes)
    fail, though both are far under 250 characters."""
    item = _listing("kurta_002", "amazon")
    letter = "\u0915"  # DEVANAGARI LETTER KA
    assert len(letter.encode("utf-8")) == 3
    assert _byte_errors(_with_hidden(item, [letter * 83])) == []
    (error,) = _byte_errors(_with_hidden(item, [letter * 84]))
    assert error.field == "keywords.hidden_terms"


@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_hidden_terms_no_flipkart_byte_rule(category: str) -> None:
    item = _listing(CATEGORY_PRODUCT[category], "flipkart")
    assert _byte_errors(_with_hidden(item, ["a" * 251])) == []


# ---- contact details and markup: no URL, email, phone number or HTML tag in listing text ----
#
# One platform `*` banned_terms row (pack `contact`, match `regex`) per seed RuleSet. A hit is a
# BANNED_TERM error, so the correction loop rewrites the text and a residual hit ends `blocked`.

CONTACT_RULE_IDS = {f"5eed0000-0000-4000-8000-000{m}000{c}0008" for m in (1, 2) for c in (1, 2, 3)}
CONTACT_HITS = (
    "Order direct at www.brand.in",
    "See https://example.org/kurta",
    "Visit brandstore.com for more",
    "Mail sales@brand.co.in today",
    "WhatsApp +91 98765 43210",
    "Call 9876543210 to order",
    "Call 98765-43210 to order",
    "Toll free 1800-123-4567",
    "Soft cotton<br>easy care",
    "<b>Pure</b> cotton",
    "WhatsApp wa.me/919876543210",
    "Chat on WA.ME/919876543210",
    "Order at bit.ly/kurta",
    "Shop tinyurl.com/kurta",
    "Find us at Brand.me",
    "WhatsApp 919876543210",
    "Call 09876543210 to order",
    "Call 098765 43210 to order",
    "Line one<br/>line two",
    "<a href='x'>Shop</a>",
    "Call 987 654 3210 to order",
    "Call 987-654-3210 to order",
    "Call 98765\u201343210 to order",
    "Call 98765\u200b43210 to order",
)
CONTACT_CLEAN = (
    "Pure cotton. In the box: 1 kurta, 1 dupatta",
    "Size 3XL, 100% cotton, 110 cm length",
    "A 1.5 m cable, e.g. for bedside tables",
    "EAN 8901234567890, model 2024",
    "Fits a 38-40 inch chest; weight < 1 kg",
    "Festive wear.Co-ord set",
    "Festive wear.co-ord set",
    "Fits 5.5 ft.In stock",
    "Pack of 3 (S<M>L)",
    "GTIN 08901234567890",
)


def _with_text(item: CanonicalListing, path: str, text: str) -> CanonicalListing:
    content, keywords = item.content, item.keywords
    if path == "content.title":
        content = content.model_copy(update={"title": f"{content.title} {text}"})
    elif path == "content.bullets[0]":
        content = content.model_copy(update={"bullets": [text, *content.bullets[1:]]})
    elif path == "content.description":
        content = content.model_copy(update={"description": text})
    elif path == "keywords.search_terms[0]":
        keywords = keywords.model_copy(update={"search_terms": [text]})
    else:
        keywords = keywords.model_copy(update={"hidden_terms": [text]})
    return item.model_copy(update={"content": content, "keywords": keywords})


def _contact_errors(item: CanonicalListing) -> list[Any]:
    return [e for e in _run(item).errors if str(e.rule_id) in CONTACT_RULE_IDS]


CONTACT_PATHS = (
    "content.title",
    "content.bullets[0]",
    "content.description",
    "keywords.search_terms[0]",
    "keywords.hidden_terms[0]",
)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize("path", CONTACT_PATHS)
def test_rules_seed_contact_details_blocked(product: str, marketplace: str, path: str) -> None:
    base = _listing(product, marketplace)
    assert _contact_errors(base) == []
    for text in CONTACT_HITS:
        errors = _contact_errors(_with_text(base, path, text))
        assert [e.field for e in errors] == [path], text
        (error,) = errors
        assert error.error_code.value == "BANNED_TERM"
        assert error.severity.value == "error"
        assert error.retry_class is not None and error.retry_class.value == "ai_correct"
        assert error.suggestion


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("path", CONTACT_PATHS)
def test_rules_seed_contact_details_spare_ordinary_text(marketplace: str, path: str) -> None:
    base = _listing("kurta_002", marketplace)
    for text in CONTACT_CLEAN:
        assert _contact_errors(_with_text(base, path, text)) == [], text


# The adapters send both keyword lists as one space-joined value (Amazon `generic_keyword`,
# Flipkart `keywords`), so a phone number split across terms reaches the marketplace whole. The
# `*` contact row also reads that joined value, built by the adapters' own helper.
SPLIT_KEYWORDS: tuple[tuple[list[str], list[str]], ...] = (
    (["98765", "43210"], []),
    (["kurta", "98765"], ["43210"]),
    (["call +91"], ["9"]),
    (["98765"], ["43210"]),
    (["987", "654", "3210"], []),
    (["9876543", "210"], []),
    (["98", "76", "54", "32", "10"], []),
    (["kurta 987-654"], ["3210"]),
)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize(("search", "hidden"), SPLIT_KEYWORDS)
def test_rules_seed_contact_split_across_keywords_blocked(
    marketplace: str, search: list[str], hidden: list[str]
) -> None:
    from app.services.rules_engine.text import submitted_keywords

    base = _listing("kurta_002", marketplace)
    keywords = base.keywords.model_copy(update={"search_terms": search, "hidden_terms": hidden})
    item = base.model_copy(update={"keywords": keywords})
    errors = _contact_errors(item)
    assert [e.field for e in errors] == ["keywords.search_terms"], submitted_keywords(keywords)
    (error,) = errors
    assert error.error_code.value == "BANNED_TERM"
    assert error.value_submitted == submitted_keywords(keywords)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_contact_whole_term_reported_once(marketplace: str) -> None:
    """A term that already holds the number gives its own item and no second, joined one."""
    base = _listing("kurta_002", marketplace)
    keywords = base.keywords.model_copy(update={"search_terms": ["kurta", "9876543210"]})
    errors = _contact_errors(base.model_copy(update={"keywords": keywords}))
    assert [e.field for e in errors] == ["keywords.search_terms[1]"]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_contact_split_keywords_spare_ordinary_terms(marketplace: str) -> None:
    base = _listing("kurta_002", marketplace)
    keywords = base.keywords.model_copy(
        update={"search_terms": ["kurta set", "size 38", "pack of 3"], "hidden_terms": ["110 cm"]}
    )
    assert _contact_errors(base.model_copy(update={"keywords": keywords})) == []
    sizes = base.keywords.model_copy(
        update={"search_terms": ["kurta 38 40 42", "44 46"], "hidden_terms": ["2024", "3 pc"]}
    )
    assert _contact_errors(base.model_copy(update={"keywords": sizes})) == []


# ---- claims: certification, performance and handmade claims need a backing fact ----
#
# Platform `*` claim_requires_attribute rows (UNSUPPORTED_CLAIM, severity error) per seed RuleSet:
# the product rule "never invent certifications or performance claims" is enforced by the final
# arbiter even with a stub or replacement pipeline. Backing: `certifications` for certification
# claims, `performance_claims` for performance claims, `construction` naming the hand process.
CLAIM_PACKS = ("claim_certification", "claim_performance", "claim_handmade")
CLAIM_CASES = (
    ("Organic cotton kurta", "claim_certification"),
    ("GOTS certified fabric", "claim_certification"),
    ("Certified safe dyes", "claim_certification"),
    ("OEKO-TEX tested", "claim_certification"),
    ("Waterproof finish", "claim_performance"),
    ("Water-resistant shade", "claim_performance"),
    ("Anti-bacterial lining", "claim_performance"),
    ("Antimicrobial fabric", "claim_performance"),
    ("Handmade by artisans", "claim_handmade"),
    ("Hand-crafted brass", "claim_handmade"),
    ("Pure handloom weave", "claim_handmade"),
    ("Hand-loom weave", "claim_handmade"),
    ("Hand\u2011made by artisans", "claim_handmade"),
    ("Odor-resistant finish", "claim_performance"),
    ("UV-protection fabric", "claim_performance"),
    ("UV protected shade", "claim_performance"),
    ("Water repellent coat", "claim_performance"),
    ("Hypoallergenic lining", "claim_performance"),
    ("anti\u2013bacterial lining", "claim_performance"),
    ("Or\u200bganic cotton", "claim_certification"),
    ("Eco friendly dyes", "claim_certification"),
    ("Eco-friendly dyes", "claim_certification"),
)
CLAIM_CLEAN = (
    "Hand wash separately in cold water",
    "Inorganic pigment print",
    "Handle with care",
    "Water-based print, machine wash",
    "Hand-wash separately",
    "Eco wash, colour fast",
)
BACKING: dict[str, dict[str, Any]] = {
    "claim_certification": {"certifications": ["GOTS"]},
    "claim_performance": {"performance_claims": ["water resistant"]},
    "claim_handmade": {"construction": "handmade"},
}
CLAIM_PATHS = ("content.title", "content.bullets[0]", "content.description")


def _claim_errors(
    item: CanonicalListing, product: dict[str, Any] | None = None
) -> list[tuple[str, str | None]]:
    """(field, pack) of each UNSUPPORTED_CLAIM item."""
    resolved = _resolved(item.marketplace.value, item.category)
    packs = {r.id: r.rule_def.pack for r in resolved.rules}
    report = evaluate(item, resolved, product=product)
    return [
        (e.field, packs.get(e.rule_id) if e.rule_id else None)
        for e in report.errors
        if e.error_code.value == "UNSUPPORTED_CLAIM"
    ]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("category", list(CATEGORY_PRODUCT))
def test_rules_seed_claim_rows_each_ruleset(marketplace: str, category: str) -> None:
    rows = [r for r in _seed()[(marketplace, category)].rules if r.rule_def.pack in CLAIM_PACKS]
    assert sorted(r.rule_def.pack for r in rows) == sorted(CLAIM_PACKS)
    for row in rows:
        assert row.field == "*" and row.tenant_id is None and row.severity.value == "error"
        assert row.rule_def.type == "claim_requires_attribute"
        assert row.rule_def.error_code.value == "UNSUPPORTED_CLAIM"
        assert row.rule_def.policy_ref is not None and row.rule_def.suggestion


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("path", CLAIM_PATHS)
def test_rules_seed_unbacked_claims_blocked(marketplace: str, path: str) -> None:
    for category, product in CATEGORY_PRODUCT.items():
        base = _listing(product, marketplace)
        assert _claim_errors(base) == [], category
        for text, pack in CLAIM_CASES:
            item = _with_text(base, path, text)
            assert _claim_errors(item) == [(path, pack)], (category, text)
            rep = _run(item)
            assert rep.status == "failed"
            (error,) = [e for e in rep.errors if e.error_code.value == "UNSUPPORTED_CLAIM"]
            assert error.retry_class is not None and error.retry_class.value == "ai_correct"


CLAIM_SPLIT_KEYWORDS: tuple[tuple[list[str], list[str], str], ...] = (
    (["water", "proof"], [], "claim_performance"),
    (["kurta", "hand"], ["made"], "claim_handmade"),
    (["fair"], ["trade"], "claim_certification"),
)


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize(("search", "hidden", "pack"), CLAIM_SPLIT_KEYWORDS)
def test_rules_seed_claim_split_across_keywords_blocked(
    marketplace: str, search: list[str], hidden: list[str], pack: str
) -> None:
    """A claim phrase split across terms reaches the marketplace whole in the joined value."""
    base = _listing("kurta_002", marketplace)
    keywords = base.keywords.model_copy(update={"search_terms": search, "hidden_terms": hidden})
    item = base.model_copy(update={"keywords": keywords})
    assert _claim_errors(item) == [("keywords.search_terms", pack)]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_claim_whole_keyword_reported_once(marketplace: str) -> None:
    base = _listing("kurta_002", marketplace)
    keywords = base.keywords.model_copy(update={"search_terms": ["kurta", "waterproof"]})
    item = base.model_copy(update={"keywords": keywords})
    assert _claim_errors(item) == [("keywords.search_terms[1]", "claim_performance")]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_backed_claims_pass(marketplace: str) -> None:
    """The backing fact on the product (and the listing) lets the claim stand."""
    for product in CATEGORY_PRODUCT.values():
        base = _listing(product, marketplace)
        for text, pack in CLAIM_CASES:
            item = _with_text(base, "content.description", text)
            backed = item.model_copy(update={"attributes": {**item.attributes, **BACKING[pack]}})
            facts = {"attributes": dict(BACKING[pack])}
            assert _claim_errors(backed, facts) == [], text


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_claim_backing_must_be_a_product_fact(marketplace: str) -> None:
    """A listing attribute the model wrote is not a fact: with the product's facts at hand, the
    claim needs the attribute among them."""
    base = _listing("kurta_002", marketplace)
    item = _with_text(base, "content.title", "GOTS certified")
    attributes = {**item.attributes, "certifications": ["GOTS"]}
    invented = item.model_copy(update={"attributes": attributes})
    assert _claim_errors(invented, {"attributes": {}}) == [("content.title", "claim_certification")]
    assert _claim_errors(invented, {"attributes": {"certifications": ["GOTS"]}}) == []


@pytest.mark.parametrize("marketplace", MARKETPLACES)
def test_rules_seed_handmade_needs_a_hand_process(marketplace: str) -> None:
    item = _with_text(_listing("kurta_002", marketplace), "content.title", "Handmade")
    machine = {"construction": "machine stitched"}
    other = item.model_copy(update={"attributes": {**item.attributes, **machine}})
    assert _claim_errors(other, {"attributes": machine}) == [("content.title", "claim_handmade")]


@pytest.mark.parametrize("marketplace", MARKETPLACES)
@pytest.mark.parametrize("path", CLAIM_PATHS)
def test_rules_seed_claims_spare_ordinary_text(marketplace: str, path: str) -> None:
    base = _listing("kurta_002", marketplace)
    for text in CLAIM_CLEAN:
        assert _claim_errors(_with_text(base, path, text)) == [], text
