"""S22 T5, T6 (AC8, AC15; D3, D4; C6-C8): the pure Flipkart mapping, its checks and `preview()`."""

from __future__ import annotations

import ast
import dataclasses
import uuid
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.adapters.flipkart_support import fk_family, fk_item, listing, template

MAPPING = Path(__file__).resolve().parents[2] / "app/adapters/flipkart"


def _attrs(item: object) -> dict[str, list[dict[str, object]]]:
    from app.adapters.flipkart.mapping import family_payloads

    (one,) = family_payloads(item)  # type: ignore[arg-type]
    attributes: dict[str, list[dict[str, object]]] = one.payload["attributes"]
    return attributes


def _values(attributes: dict[str, list[dict[str, object]]], name: str) -> list[object]:
    return [entry["value"] for entry in attributes[name]]


# ---- T5: values ----


@pytest.mark.parametrize("raw", ["navy blue", " NAVY BLUE ", "Navy Blue", "navy"])
def test_flipkart_mapping_value_map(raw: str) -> None:
    """L10635: canonical `color="navy blue"` -> Flipkart `Colour="Navy Blue"`."""
    assert _values(_attrs(fk_item(listing(color=raw))), "Colour") == ["Navy Blue"]


def test_flipkart_mapping_allowed_passthrough() -> None:
    attributes = _attrs(fk_item(listing(color="maroon", fit="slim")))
    assert _values(attributes, "Colour") == ["Maroon"]
    assert _values(attributes, "Fit") == ["Slim"]


def test_flipkart_mapping_content_fields() -> None:
    sent = fk_item()
    attributes = _attrs(sent)
    body = sent.listing
    assert _values(attributes, "title") == [body.content.title]
    assert _values(attributes, "key_features") == list(body.content.bullets)
    assert _values(attributes, "description") == [body.content.description]


def test_flipkart_mapping_keywords_merged_dedup() -> None:
    body = listing().model_copy(
        update={
            "keywords": listing().keywords.model_copy(
                update={"search_terms": ["Cotton Kurta", "kurta"], "hidden_terms": ["cotton kurta"]}
            )
        }
    )
    assert _values(_attrs(fk_item(body)), "keywords") == ["Cotton Kurta kurta"]


def test_flipkart_mapping_images_capped_rest_skipped() -> None:
    from app.adapters.flipkart.mapping import family_payloads

    base = listing()
    first = base.images[0]
    images = [first.model_copy(update={"image_id": uuid.uuid4()}) for _ in range(11)]
    body = base.model_copy(update={"images": images})
    sent = fk_item(body)
    (one,) = family_payloads(sent)
    assert len(one.payload["attributes"]["images"]) == 10
    assert "images[10]" in one.skipped and "images[9]" in one.submitted


def test_flipkart_mapping_empty_bullets_not_sent() -> None:
    from app.adapters.flipkart.mapping import family_payloads

    body = listing()
    body = body.model_copy(update={"content": body.content.model_copy(update={"bullets": []})})
    (one,) = family_payloads(fk_item(body))
    assert "key_features" not in one.payload["attributes"]
    assert "content.bullets" in one.skipped


def test_flipkart_mapping_unicode_title_kept() -> None:
    body = listing()
    title = "सूती कुर्ता सेट"
    body = body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    assert _values(_attrs(fk_item(body)), "title") == [title]


def test_flipkart_mapping_reverse_index_round_trip() -> None:
    from app.adapters.flipkart.mapping import family_payloads, reverse_index

    sent = fk_item()
    (one,) = family_payloads(sent)
    index = reverse_index(template().template)
    for name in one.payload["attributes"]:
        assert name in index, name
    for path in one.submitted:
        assert any(path in feeds for feeds in index.values()), path
    assert index["keywords"] == ("keywords.hidden_terms", "keywords.search_terms")
    assert index["Colour"] == ("attributes.color",)


_FORBIDDEN_KEYS = st.sampled_from(["mrp", "selling_price", "stock", "hsn", "package_weight"])
_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), max_size=30)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(extra=st.dictionaries(_FORBIDDEN_KEYS, _TEXT, max_size=5), color=_TEXT, title=_TEXT)
def test_flipkart_payload_content_only(extra: dict[str, str], color: str, title: str) -> None:
    """[prop] whatever the listing carries, no price, MRP, stock, HSN, package or fulfilment key
    is ever sent."""
    from app.adapters.base import AdapterTemplate
    from app.adapters.flipkart.constants import FORBIDDEN_ATTRIBUTES
    from app.adapters.flipkart.mapping import family_payloads
    from mkos_schemas.jobs import AttributeSpec

    body = listing(color=color or None, **extra)
    if title.strip():
        body = body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    # a template that (wrongly) maps the commercial keys is still refused
    base = template()
    specs = dict(base.template.specs)
    for key in extra:
        specs[key] = AttributeSpec(
            requirement="optional", marketplace_field=key.upper(), value_type="text"
        )
    leaky = AdapterTemplate(
        base.product_type, None, base.template.model_copy(update={"specs": specs})
    )
    for sent in (fk_item(body, template=leaky), fk_family(body, template=leaky)):
        for one in family_payloads(sent):
            keys = {k.casefold() for k in one.payload["attributes"]} | set(one.payload)
            assert not keys & FORBIDDEN_ATTRIBUTES, keys & FORBIDDEN_ATTRIBUTES


@pytest.mark.parametrize("name", ["mapping.py", "constants.py", "endpoint.py"])
def test_flipkart_mapping_imports_no_io(name: str) -> None:
    tree = ast.parse((MAPPING / name).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"httpx", "socket", "sqlalchemy", "psycopg", "asyncio"}, roots


# ---- T6: checks ----


def _codes(sent: object) -> list[tuple[str, str]]:
    from app.adapters.flipkart.mapping import check

    return [(e.field, e.error_code.value) for e in check(sent)]  # type: ignore[arg-type]


def test_flipkart_mapping_passes_clean_listing() -> None:
    assert _codes(fk_item()) == []
    assert _codes(fk_family()) == []


def test_flipkart_mapping_missing_required() -> None:
    assert ("attributes.fabric_type", "MISSING_ATTRIBUTE") in _codes(
        fk_item(listing(fabric_type=None))
    )


def test_flipkart_mapping_blank_required() -> None:
    assert ("attributes.fabric_type", "MISSING_ATTRIBUTE") in _codes(
        fk_item(listing(fabric_type="  "))
    )


def test_flipkart_mapping_value_not_allowed() -> None:
    from app.adapters.flipkart.mapping import check

    (problem,) = check(fk_item(listing(color="ultraviolet")))
    assert problem.field == "attributes.color" and problem.error_code.value == "VALUE_NOT_ALLOWED"
    assert problem.value_submitted == "ultraviolet"
    assert problem.source.value == "flipkart_adapter"
    assert problem.marketplace is not None and problem.marketplace.value == "flipkart"


def test_flipkart_mapping_category_not_mapped_lamps() -> None:
    from app.adapters.base import AdapterTemplate
    from mkos_schemas.jobs import CategoryTemplate

    lamps = CategoryTemplate.model_validate(
        {"marketplace": "flipkart", "category": "home_decor_lamps"}
    )
    sent = fk_item(template=AdapterTemplate("LAMP", None, lamps))
    assert _codes(sent) == [("listing", "CATEGORY_NOT_MAPPED")]
    assert _codes(fk_item(template=None)) == [("listing", "CATEGORY_NOT_MAPPED")]


def test_flipkart_mapping_null_product_type() -> None:
    from app.adapters.base import AdapterTemplate

    sent = fk_item(template=AdapterTemplate(None, None, template().template))
    assert _codes(sent) == [("listing", "CATEGORY_NOT_MAPPED")]


@pytest.mark.parametrize(
    ("sku", "bad"),
    [("A,B", True), ("A/B", True), (".", True), ("..", True), ("a.b", False)],
    ids=["comma", "slash", "dot", "dotdot", "a.b"],
)
def test_flipkart_sku_reserved_chars(sku: str, bad: bool) -> None:
    codes = _codes(fk_item(sku=sku))
    assert (("listing", "VALUE_NOT_ALLOWED") in codes) is bad, codes


def test_flipkart_sku_reserved_chars_on_a_child() -> None:
    sent = fk_family()
    first = dataclasses.replace(sent.items[0], seller_sku="FK,1")
    sent = dataclasses.replace(sent, items=(first, *sent.items[1:]))
    assert ("variants[FK,1]", "VALUE_NOT_ALLOWED") in _codes(sent) or (
        "variants",
        "VALUE_NOT_ALLOWED",
    ) in _codes(sent)


_SKU = st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ0123456789-", min_size=1, max_size=12)


@settings(max_examples=60, deadline=None)
@given(sku=_SKU, color=st.sampled_from(["navy blue", "maroon", "ultraviolet", " "]))
def test_flipkart_preview_deterministic(sku: str, color: str) -> None:
    """[prop] the same input always previews the same payloads and report."""
    from app.adapters.flipkart.mapping import preview

    sent = fk_item(listing(color=color), sku=sku)
    first, again = preview(sent), preview(sent)
    assert first == again
    assert [i.seller_sku for i in first.items] == [sku]


def test_registry_flipkart_previewer() -> None:
    from app.adapters.flipkart.mapping import preview
    from app.adapters.registry import previewer

    assert previewer("flipkart") is preview
    result = previewer("flipkart")(fk_item())
    assert result.product_type == "kurta" and result.report.status == "passed"


def test_family_has_parent() -> None:
    from app.adapters.registry import family_has_parent

    assert family_has_parent("amazon") is True
    assert family_has_parent("flipkart") is False
