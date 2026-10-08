"""S20 T6, T7 (AC6, AC7; D1; C6, C10, C16): the pure Amazon mapping."""

from __future__ import annotations

import ast
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.listing import ImageRef
from tests.adapters.amazon_support import amazon_family, amazon_item, listing, template

AMAZON = Path(__file__).resolve().parents[2] / "app" / "adapters" / "amazon"


def _attrs(sent: Any, index: int = 0) -> dict[str, Any]:
    from app.adapters.amazon.mapping import family_payloads

    return family_payloads(sent)[index].payload["attributes"]  # type: ignore[no-any-return]


def _codes(sent: Any) -> list[tuple[str, str]]:
    from app.adapters.amazon.mapping import check

    return [(e.field, e.error_code.value) for e in check(sent)]


# ---- T6: values ----


def test_amazon_mapping_value_map() -> None:
    for raw in ("navy blue", " NAVY BLUE ", "Navy Blue"):
        assert _attrs(amazon_item(listing(color=raw)))["color"][0]["value"] == "Navy"


def test_amazon_mapping_allowed_passthrough() -> None:
    attrs = _attrs(amazon_item(listing(color="maroon", fit="relaxed")))
    assert attrs["color"][0]["value"] == "Maroon"  # the template's spelling
    assert attrs["fit_type"][0]["value"] == "Relaxed"


def test_amazon_mapping_measurement_unit() -> None:
    sent = amazon_item(
        listing(product_weight=180).model_copy(update={"category": "tshirts"}),
        template=template("tshirts"),
    )
    assert _attrs(sent)["item_weight"] == [
        {"value": 180, "unit": "grams", "marketplace_id": "A21TJRUUN4KGV"}
    ]


def test_amazon_mapping_content_fields() -> None:
    sent = amazon_item()
    attrs = _attrs(sent)
    body = sent.listing
    assert attrs["item_name"] == [
        {"value": body.content.title, "marketplace_id": "A21TJRUUN4KGV", "language_tag": "en_IN"}
    ]
    assert [b["value"] for b in attrs["bullet_point"]] == body.content.bullets
    assert attrs["product_description"][0]["value"] == body.content.description


def test_amazon_mapping_keywords_merged_dedup() -> None:
    body = listing().model_copy(
        update={
            "keywords": listing().keywords.model_copy(
                update={
                    "search_terms": ["kurta", "Cotton Set"],
                    "hidden_terms": ["KURTA", "indigo"],
                }
            )
        }
    )
    attrs = _attrs(amazon_item(body))
    assert attrs["generic_keyword"] == [
        {
            "value": "kurta Cotton Set indigo",
            "marketplace_id": "A21TJRUUN4KGV",
            "language_tag": "en_IN",
        }
    ]


def _images(n: int) -> list[ImageRef]:
    return [
        ImageRef(image_id=uuid.uuid4(), slot="main" if i == 0 else "secondary", position=i)
        for i in range(n)
    ]


def test_amazon_mapping_images_main_then_others() -> None:
    from app.adapters.amazon.mapping import family_payloads

    body = listing().model_copy(update={"images": _images(3)})
    sent = amazon_item(body)
    attrs = _attrs(sent)
    refs = sent.image_refs
    assert attrs["main_product_image_locator"][0]["media_location"] == refs[body.images[0].image_id]
    assert (
        attrs["other_product_image_locator_2"][0]["media_location"] == refs[body.images[2].image_id]
    )
    (mapped,) = family_payloads(sent)
    assert {"images[0]", "images[1]", "images[2]"} <= set(mapped.submitted)


def test_amazon_mapping_images_over_nine_skipped() -> None:
    from app.adapters.amazon.mapping import family_payloads

    body = listing().model_copy(update={"images": _images(11)})
    (mapped,) = family_payloads(amazon_item(body))
    assert {"images[9]", "images[10]"} <= set(mapped.skipped)
    assert "images[8]" in mapped.submitted
    assert "other_product_image_locator_8" in mapped.payload["attributes"]


def test_amazon_mapping_image_without_locator_skipped() -> None:
    from app.adapters.amazon.mapping import family_payloads

    (mapped,) = family_payloads(amazon_item(image_refs={}))
    assert {"images[0]", "images[1]"} <= set(mapped.skipped)
    assert "main_product_image_locator" not in mapped.payload["attributes"]


def test_amazon_mapping_empty_bullets_not_sent() -> None:
    body = listing()
    blank = body.model_copy(
        update={"content": body.content.model_copy(update={"bullets": ["Soft", "   "]})}
    )
    assert [b["value"] for b in _attrs(amazon_item(blank))["bullet_point"]] == ["Soft"]


def test_amazon_mapping_unicode_title_kept() -> None:
    body = listing()
    title = "सूती कुर्ता सेट — Women\u2019s Cotton Kurta"
    sent = amazon_item(
        body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    )
    assert _attrs(sent)["item_name"][0]["value"] == title


def test_amazon_mapping_reverse_index_round_trip() -> None:
    from app.adapters.amazon.mapping import reverse_index

    index = reverse_index(template().template)
    assert set(index["generic_keyword"]) == {"keywords.search_terms", "keywords.hidden_terms"}
    assert index["item_name"] == ("content.title",)
    assert index["color"] == ("attributes.color",)
    assert index["sleeve_type"] == ("attributes.sleeve_length",)
    assert index["main_product_image_locator"] == ("images[0]",)
    for name, spec_path in (
        (s.marketplace_field, f"attributes.{k}") for k, s in template().template.specs.items()
    ):
        assert spec_path in index[str(name)]


def test_amazon_themes_cover_vocabulary() -> None:
    from app.adapters.amazon.constants import AMAZON_THEMES, NEUTRAL_THEME_AXES

    assert set(NEUTRAL_THEME_AXES) == {"size", "color", "size_color"}
    for axes in NEUTRAL_THEME_AXES.values():
        assert axes in AMAZON_THEMES


def test_amazon_mapping_imports_no_io() -> None:
    for name in ("mapping.py", "constants.py", "endpoint.py"):
        path = AMAZON / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        assert not modules & {"httpx", "socket", "sqlalchemy", "asyncio"}, (name, modules)


# ---- T7: checks ----


def test_amazon_mapping_missing_required() -> None:
    assert ("attributes.fabric_type", "MISSING_ATTRIBUTE") in _codes(
        amazon_item(listing(fabric_type=None))
    )


def test_amazon_mapping_blank_required() -> None:
    assert ("attributes.color", "MISSING_ATTRIBUTE") in _codes(amazon_item(listing(color="  ")))


def test_amazon_mapping_value_not_allowed() -> None:
    from app.adapters.amazon.mapping import check

    (problem,) = check(amazon_item(listing(color="ultraviolet")))
    assert (problem.field, problem.error_code) == ("attributes.color", ErrorCode.VALUE_NOT_ALLOWED)
    assert problem.value_submitted == "ultraviolet"
    assert problem.source == "amazon_adapter" and problem.marketplace == "amazon"


def test_amazon_mapping_unsupported_skipped() -> None:
    from app.adapters.amazon.mapping import check, family_payloads

    sent = amazon_item(listing(material="organic cotton"))
    (mapped,) = family_payloads(sent)
    assert "attributes.material" in mapped.skipped  # an alias, not in the mapping
    assert check(sent) == []


def test_amazon_mapping_no_template_category_not_mapped() -> None:
    assert _codes(amazon_item(template=None)) == [("listing", "CATEGORY_NOT_MAPPED")]


def test_amazon_mapping_null_product_type_category_not_mapped() -> None:
    from app.adapters.base import AdapterTemplate

    t = template()
    assert _codes(amazon_item(template=AdapterTemplate(None, None, t.template))) == [
        ("listing", "CATEGORY_NOT_MAPPED")
    ]


def test_amazon_mapping_v1_template_without_fields_not_mapped() -> None:
    from app.adapters.base import AdapterTemplate
    from mkos_schemas.jobs import CategoryTemplate

    v1 = CategoryTemplate(marketplace="amazon", category="ethnic_wear_kurta_sets")
    sent = amazon_item(template=AdapterTemplate("KURTA", None, v1))
    assert _codes(sent) == [("listing", "CATEGORY_NOT_MAPPED")]


def test_amazon_mapping_lamps_unmapped() -> None:
    from app.adapters.base import AdapterTemplate
    from mkos_schemas.jobs import CategoryTemplate

    lamps = CategoryTemplate(marketplace="amazon", category="home_decor_lamps")
    body = listing().model_copy(update={"category": "home_decor_lamps"})
    sent = amazon_item(body, template=AdapterTemplate("LAMP", None, lamps))
    assert _codes(sent) == [("listing", "CATEGORY_NOT_MAPPED")]


@pytest.mark.parametrize("sku", [".", ".."])
def test_amazon_mapping_dot_sku_value_not_allowed(sku: str) -> None:
    from app.adapters.base import AdapterItem

    assert ("listing", "VALUE_NOT_ALLOWED") in _codes(amazon_item(sku=sku))
    fam = amazon_family()
    parent, first, *rest = fam.units()
    bad = AdapterItem(first.product_id, sku, "child", first.variant_attributes)
    sent = dataclasses.replace(fam, items=(parent, bad, *rest))
    assert (f"variants[{sku}]", "VALUE_NOT_ALLOWED") in _codes(sent)


@pytest.mark.parametrize("sku", ["a.b", ".x", "x..y"])
def test_amazon_mapping_dotted_sku_ok(sku: str) -> None:
    assert _codes(amazon_item(sku=sku)) == []


@settings(max_examples=40, deadline=None)
@given(
    color=st.sampled_from(["navy blue", "Maroon", "black", "Indigo"]),
    size=st.sampled_from(["S", "m", "XL"]),
    title=st.text(min_size=1, max_size=40).filter(lambda t: t.strip() != ""),
)
def test_preview_deterministic(color: str, size: str, title: str) -> None:
    from app.adapters.amazon.mapping import preview

    body = listing(color=color, size=size)
    body = body.model_copy(update={"content": body.content.model_copy(update={"title": title})})
    sent = amazon_item(body, sku="SKU-FIXED", key="k")

    def dump() -> str:
        got = preview(sent)
        return (
            json.dumps([dataclasses.asdict(i) for i in got.items], sort_keys=True, default=str)
            + got.report.model_dump_json()
        )

    assert dump() == dump()


def test_registry_flipkart_not_implemented() -> None:
    from app.adapters.amazon.mapping import preview
    from app.adapters.registry import previewer

    assert previewer("amazon") is preview
    with pytest.raises(NotImplementedError):
        previewer("flipkart")
