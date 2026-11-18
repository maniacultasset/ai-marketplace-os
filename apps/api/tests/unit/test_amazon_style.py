"""Prod feedback 18 Nov: Amazon refused a T-shirt create with "'Style' is required but missing".

The Amazon apparel templates send `style` (required); the seller overlay fills it from the
seller's own `style`, else from the seller's own facts (never the AI); a missing one is a Rules
Engine MISSING_ATTRIBUTE the seller answers; an Amazon issue on `style` lands on
`attributes.style` (the seller's lane), even when the version pinned an older template.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest
from app.core.resolved_facts import ProductFacts, ResolvedFact
from app.services.seller_facts import seller_overlay, style_value
from mkos_schemas.jobs import CategoryTemplate
from mkos_schemas.listing import CanonicalListing
from tests.adapters.contract import sample_listing

BOTTOMS = ("jeans", "trousers", "track_pants", "shorts", "skirts")


def template(category: str, marketplace: str = "amazon") -> CategoryTemplate:
    from app.db.seed_templates import flipkart_seed_templates, seed_templates

    seeds = seed_templates() if marketplace == "amazon" else flipkart_seed_templates()
    (seed,) = [s for s in seeds if s.template.category == category]
    return seed.template


def facts(source: str = "seller", **attrs: Any) -> ProductFacts:
    return ProductFacts(
        product_id=uuid.uuid4(),
        sku="SKU-1",
        level="standalone",
        attributes={k: ResolvedFact(key=k, value=v, source=source) for k, v in attrs.items()},
    )


def listing(**attributes: Any) -> CanonicalListing:
    return sample_listing().model_copy(update={"attributes": attributes, "attribute_sources": {}})


# ---- the templates ----


@pytest.mark.parametrize("category", ["tshirts", "ethnic_wear_kurta_sets", *BOTTOMS])
def test_amazon_apparel_requires_style_sent_as_amazon_style(category: str) -> None:
    from app.services.rules_engine import requirement_levels

    t = template(category)
    assert t.specs["style"].marketplace_field == "style"
    assert t.specs["style"].requirement == "required"
    assert requirement_levels(t)["style"] == "required"
    fields = [s.marketplace_field for s in t.specs.values() if s.marketplace_field == "style"]
    assert fields == ["style"]  # one Amazon `style`, never the kurta type as well


@pytest.mark.parametrize("category", ["tshirts", "ethnic_wear_kurta_sets", *BOTTOMS])
def test_flipkart_templates_have_no_style(category: str) -> None:
    assert "style" not in template(category, "flipkart").specs


def test_dictionary_style_key() -> None:
    from app.services.seller_facts import _ENTRIES

    entry = _ENTRIES["style"]
    assert entry["owner_levels"] == ["style", "standalone"]
    assert entry["allowed_sources"] == ["seller", "derived"]
    assert entry["is_critical"] is False and entry["value_type"] == "text"


# ---- the value ----


def test_seller_style_wins_over_everything() -> None:
    got = style_value(facts(style="Polo", neckline="round", fit="Slim"), template("tshirts"))
    assert got == ("Polo", "style")


@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({"neckline": "round", "fit": "Slim"}, ("Round Neck", "neckline")),
        ({"neckline": "V-Neck"}, ("V-Neck", "neckline")),
        ({"fit": "Slim"}, ("Slim Fit", "fit")),
        ({"fit": "Regular Fit"}, ("Regular Fit", "fit")),
        ({}, None),
    ],
)
def test_tshirt_style_from_neckline_then_fit(
    attrs: dict[str, str], expected: tuple[str, str] | None
) -> None:
    assert style_value(facts(**attrs), template("tshirts")) == expected


def test_kurta_style_is_the_kurta_type_else_missing() -> None:
    t = template("ethnic_wear_kurta_sets")
    assert style_value(facts(kurta_type="Anarkali", fit="Slim"), t) == ("Anarkali", "kurta_type")
    assert style_value(facts(neckline="round", fit="Slim"), t) is None


@pytest.mark.parametrize("category", BOTTOMS)
def test_bottomwear_style_from_leg_style_and_fit(category: str) -> None:
    t = template(category)
    assert style_value(facts(leg_style="Straight", fit="Slim"), t) == (
        "Straight, Slim Fit",
        "leg_style",
    )
    assert style_value(facts(fit="Slim"), t) == ("Slim Fit", "fit")
    assert style_value(facts(leg_style="Straight"), t) == ("Straight", "leg_style")
    assert style_value(facts(neckline="round"), t) is None


@pytest.mark.parametrize("source", ["ai", "marketplace", None])
def test_a_value_the_seller_did_not_give_is_never_used(source: str | None) -> None:
    t = template("tshirts")
    got = style_value(facts(source, style="Polo", neckline="round", fit="Slim"), t)  # type: ignore[arg-type]
    assert got is None


def test_no_style_spec_no_style() -> None:
    assert style_value(facts(neckline="round"), template("tshirts", "flipkart")) is None
    assert style_value(facts(neckline="round"), None) is None


def test_overlay_sets_derived_style_and_drops_the_ai_one() -> None:
    t = template("tshirts")
    out = seller_overlay(listing(style="AI Guess"), facts(neckline="round"), t)
    assert out.attributes["style"] == "Round Neck"
    assert out.attribute_sources["style"] == "neckline"
    gone = seller_overlay(listing(style="AI Guess"), facts(), t)
    assert "style" not in gone.attributes and "style" not in gone.attribute_sources


def test_overlay_leaves_style_alone_without_a_style_spec() -> None:
    out = seller_overlay(listing(style="Casual"), facts(), template("tshirts", "flipkart"))
    assert out.attributes["style"] == "Casual"


def test_overlay_is_idempotent_with_style() -> None:
    t = template("ethnic_wear_kurta_sets")
    once = seller_overlay(listing(), facts(kurta_type="Straight"), t)
    assert seller_overlay(once, facts(kurta_type="Straight"), t) == once


# ---- the Rules Engine and the payload ----


def _check(category: str, product: ProductFacts) -> list[Any]:
    from app.services.rules_engine.evaluators import Ctx
    from app.services.rules_engine.template import template_checks

    t = template(category)
    overlaid = seller_overlay(listing(color="Black", fabric_type="Cotton"), product, t)
    ctx = Ctx(listing=overlaid, template=t, product=None, account=None)
    return [d for d in template_checks(ctx) if d.field == "attributes.style"]


def test_missing_style_is_a_blocking_missing_attribute() -> None:
    (draft,) = _check("tshirts", facts())
    assert draft.code.value == "MISSING_ATTRIBUTE"
    assert draft.severity.value == "error"


def test_derived_style_passes_the_check() -> None:
    assert _check("tshirts", facts(neckline="round")) == []


# ---- an Amazon issue on `style` is the seller's (contract 5) ----


def _issue(code: str, attribute: str) -> dict[str, Any]:
    return {"code": code, "message": "'Style' is required but missing.", "severity": "ERROR",
            "attributeNames": [attribute]}  # fmt: skip


def _shirt_without_style_pin(*, current: bool) -> Any:
    """A T-shirt listing with no style, pinned to a tshirts template from before `style`."""
    from app.adapters.base import AdapterTemplate
    from tests.adapters.amazon_support import amazon_item

    v4 = template("tshirts")
    old = v4.model_copy(update={"specs": {k: s for k, s in v4.specs.items() if k != "style"}})
    body = sample_listing()
    body = body.model_copy(
        update={
            "category": "tshirts",
            "attributes": {"color": "navy blue", "fabric_type": "cotton"},
            "images": [],
        }
    )
    sent = amazon_item(body, template=AdapterTemplate("SHIRT", None, old))
    return dataclasses.replace(sent, current_template=v4 if current else None)


async def _style_field(current: bool) -> Any:
    from tests.adapters.amazon_support import amazon_adapter, seed_error_rows
    from tests.support.fake_marketplace import build_fake

    app = build_fake()
    sent = _shirt_without_style_pin(current=current)
    app.state.fake.spapi_issues[sent.seller_sku] = [_issue("90220", "style")]
    result = await amazon_adapter(app, rows=seed_error_rows()).create_listing(sent)
    return {f.field: f for f in result.fields if f.status == "failed"}


async def test_a_style_issue_lands_on_the_seller_attribute() -> None:
    failed = await _style_field(current=True)
    assert set(failed) == {"attributes.style"}
    error = failed["attributes.style"].error
    assert error is not None and error.error_code.value == "MISSING_ATTRIBUTE"


async def test_without_a_current_mapping_it_stays_on_listing() -> None:
    failed = await _style_field(current=False)
    assert set(failed) == {"listing"}


def test_the_seller_answers_a_style_issue() -> None:
    from app.services.resolution.plan import IssueView, PlanContext, label

    issue = IssueView(
        id=uuid.uuid4(),
        source="amazon_adapter",
        field_path="attributes.style",
        error_code="MISSING_ATTRIBUTE",
        error_class="missing_data",
        severity="error",
        marketplace_code="90220",
    )
    assert label(issue, PlanContext(auto_fix_enabled=True)) == ("seller", "seller_fact")
    # the same issue on `listing` (before this fix) was the Kuinn team's
    on_listing = dataclasses.replace(issue, field_path="listing")
    assert label(on_listing, PlanContext(auto_fix_enabled=True)) == ("staff", "mapping")


def test_issue_index_adds_only_unmapped_seller_attributes() -> None:
    from app.adapters.amazon.mapping import issue_index, reverse_index

    sent = _shirt_without_style_pin(current=True)
    pinned = reverse_index(sent.template.template)
    index = issue_index(sent)
    assert "style" not in pinned and index["style"] == ("attributes.style",)
    assert {k: v for k, v in index.items() if k != "style"} == dict(pinned)


# ---- a fix to a never-accepted listing pins the current template (contract 6) ----


class _Templates:
    def __init__(self, category: str) -> None:
        self.category = category

    async def pinned(self, _id: uuid.UUID) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(template=SimpleNamespace(category=self.category))


def _uow(category: str = "tshirts") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(templates=_Templates(category))


def _listing_row(**over: Any) -> Any:
    from types import SimpleNamespace

    row = {"category": "tshirts", "published_version_id": None, "live_version_id": None}
    return SimpleNamespace(**{**row, **over})


OLD, NEW = uuid.uuid4(), uuid.uuid4()


async def test_never_accepted_listing_takes_the_current_template() -> None:
    from types import SimpleNamespace

    from app.services.listing_edit import template_pin

    parent = SimpleNamespace(category_template_id=OLD)
    assert await template_pin(_uow(), _listing_row(), parent, NEW) == NEW


@pytest.mark.parametrize(
    ("over", "category"),
    [
        ({"published_version_id": uuid.uuid4()}, "tshirts"),
        ({"live_version_id": uuid.uuid4()}, "tshirts"),
        ({}, "jeans"),  # recategorised: publish still asks for the regenerate
    ],
)
async def test_accepted_or_recategorised_listing_keeps_its_pin(
    over: dict[str, Any], category: str
) -> None:
    from types import SimpleNamespace

    from app.services.listing_edit import template_pin

    parent = SimpleNamespace(category_template_id=OLD)
    assert await template_pin(_uow(category), _listing_row(**over), parent, NEW) == OLD


async def test_no_pin_or_no_current_template_is_unchanged() -> None:
    from types import SimpleNamespace

    from app.services.listing_edit import template_pin

    assert await template_pin(_uow(), _listing_row(), SimpleNamespace(category_template_id=None), NEW) is None  # fmt: skip
    assert await template_pin(_uow(), _listing_row(), SimpleNamespace(category_template_id=OLD), None) == OLD  # fmt: skip
