"""Client feedback item 5 (2026-11-12): the Amazon browse node of a category template.

The node id is the template's `external_category_id`. Each item of a mapped listing sends it as
`recommended_browse_nodes` when it is a node id (digits); otherwise nothing is sent and nothing is
guessed. The drift report checks it against a stored definition, and `templates check` reports
every Amazon template against the stored definition of its own product type."""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

import pytest
from app.adapters.amazon import constants as c
from app.adapters.amazon import mapping
from app.adapters.amazon.drift import (
    Drift,
    StoredDefinition,
    browse_node_choices,
    check_templates,
    diff,
    render_checks,
)
from app.adapters.base import AdapterTemplate
from app.db.seed_templates import seed_templates
from tests.adapters.amazon_support import amazon_family, amazon_item, template
from tests.support.fake_marketplace import ptd_fixture
from tests.unit.test_amazon_schema_check import built, ready, schema_errors

MID = mapping.DEFAULT_MARKETPLACE_ID
NODE = "1968024031"  # a test value, not a real Amazon.in node


def _with_node(node: str | None) -> AdapterTemplate:
    return dataclasses.replace(template(), external_category_id=node)


def _schema(name: str = "KURTA.schema.json") -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(ptd_fixture(name))
    return loaded


def _declaring(schema: dict[str, Any], enum: list[str] | None = None) -> dict[str, Any]:
    out = copy.deepcopy(schema)
    value: dict[str, Any] = {"type": "string"}
    if enum is not None:
        value["enum"] = enum
        value["enumNames"] = [f"Node {v}" for v in enum]
    out["properties"][c.BROWSE_NODE_ATTRIBUTE] = {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": value, "marketplace_id": {"type": "string"}},
        },
    }
    return out


def _amazon_seed() -> list[tuple[str, Any]]:
    return [
        (s.product_type, s.template)
        for s in seed_templates()
        if s.template.marketplace.value == "amazon"
    ]


# ---- the payload ----


def test_every_seeded_amazon_template_has_no_node_yet() -> None:
    """No Amazon.in node id is in the repo: the mapping stays NULL until it is read."""
    assert all(t.external_category_id is None for _, t in _amazon_seed())


def test_no_node_sends_nothing() -> None:
    items, errors = mapping.build(amazon_item())
    assert errors == []
    assert all(c.BROWSE_NODE_ATTRIBUTE not in i.payload["attributes"] for i in items)


def test_a_node_is_sent_on_a_standalone_item() -> None:
    (item,), errors = mapping.build(amazon_item(template=_with_node(NODE)))
    assert errors == []
    assert item.payload["attributes"][c.BROWSE_NODE_ATTRIBUTE] == [
        {"value": NODE, "marketplace_id": MID}
    ]


def test_a_node_is_sent_on_every_item_of_a_family() -> None:
    items, errors = mapping.build(amazon_family(template=_with_node(f" {NODE} ")))
    assert errors == []
    assert {i.unit.item_role for i in items} == {"parent", "child"}
    for one in items:
        assert one.payload["attributes"][c.BROWSE_NODE_ATTRIBUTE] == [
            {"value": NODE, "marketplace_id": MID}
        ]


@pytest.mark.parametrize("node", ["", "   ", "kurtas", "1968-024", "1" * 21])
def test_a_value_that_is_not_a_node_id_is_never_sent(node: str) -> None:
    (item,), _ = mapping.build(amazon_item(template=_with_node(node)))
    assert c.BROWSE_NODE_ATTRIBUTE not in item.payload["attributes"]
    assert mapping.browse_node(_with_node(node)) is None


def test_an_unmapped_template_sends_no_node() -> None:
    unmapped = dataclasses.replace(_with_node(NODE), product_type=None)
    assert mapping.browse_node(unmapped) is None
    assert mapping.browse_node(None) is None


async def test_a_node_the_definition_declares_passes_the_schema_check() -> None:
    schema = _declaring(_schema("KURTA.listing.schema.json"))
    _, errors = built(amazon_item(template=_with_node(NODE)), await ready(schema))
    assert schema_errors(errors) == []


# ---- drift ----


def _kurta(node: str | None) -> list[Any]:
    (seed,) = [t for pt, t in _amazon_seed() if pt == "KURTA"]
    return [seed.model_copy(update={"external_category_id": node})]


def test_no_node_no_browse_node_drift() -> None:
    assert diff(_schema(), _kurta(None), "KURTA") == []


def test_a_node_the_schema_does_not_declare_is_drift() -> None:
    assert diff(_schema(), _kurta(NODE), "KURTA") == [
        Drift(
            "unknown_attribute",
            c.BROWSE_NODE_ATTRIBUTE,
            "ethnic_wear_kurta_sets -> not declared by KURTA",
        )
    ]


def test_a_declared_node_without_an_enum_is_fine() -> None:
    assert diff(_declaring(_schema()), _kurta(NODE), "KURTA") == []


def test_a_node_outside_the_enum_is_drift_and_value_free() -> None:
    (drift,) = diff(_declaring(_schema(), ["111", "222"]), _kurta(NODE), "KURTA")
    assert (drift.kind, drift.attribute) == ("unknown_value", c.BROWSE_NODE_ATTRIBUTE)
    assert "2 allowed" in drift.detail and NODE not in drift.detail
    assert diff(_declaring(_schema(), ["111", NODE]), _kurta(NODE), "KURTA") == []


def test_a_node_that_is_not_digits_is_drift() -> None:
    (drift,) = diff(_declaring(_schema()), _kurta("kurtas"), "KURTA")
    assert drift == Drift(
        "unknown_value",
        c.BROWSE_NODE_ATTRIBUTE,
        "ethnic_wear_kurta_sets browse node is not a node id",
    )


def test_a_required_browse_node_is_fed_only_when_a_node_is_set() -> None:
    schema = _declaring(_schema())
    schema["required"] = [*schema.get("required", []), c.BROWSE_NODE_ATTRIBUTE]
    assert Drift(
        "unfed_required", c.BROWSE_NODE_ATTRIBUTE, "required, and nothing here sends it"
    ) in (diff(schema, _kurta(None), "KURTA"))
    assert diff(schema, _kurta(NODE), "KURTA") == []


def test_browse_node_choices() -> None:
    assert browse_node_choices(_schema()) == []
    assert browse_node_choices(_declaring(_schema())) == []
    assert browse_node_choices(_declaring(_schema(), ["222", "111"])) == [
        ("111", "Node 111"),
        ("222", "Node 222"),
    ]
    unnamed = _declaring(_schema(), ["9"])
    unnamed["properties"][c.BROWSE_NODE_ATTRIBUTE]["items"]["properties"]["value"].pop("enumNames")
    assert browse_node_choices(unnamed) == [("9", "")]


# ---- templates check ----


def test_check_reports_a_missing_definition_as_not_checked() -> None:
    checks = check_templates(_amazon_seed(), {"KURTA": StoredDefinition("v1", _schema())})
    by_type = {one.product_type: one for one in checks}
    assert set(by_type) == {"KURTA", "SHIRT", "LAMP"}
    assert by_type["KURTA"].checked and by_type["KURTA"].drifts == ()
    assert not by_type["SHIRT"].checked and not by_type["LAMP"].checked
    text = render_checks(checks)
    assert "ethnic_wear_kurta_sets -> KURTA: definition v1, 0 drift(s); browse node not set" in text
    assert (
        "tshirts -> SHIRT: no stored definition for SHIRT, not checked; browse node not set" in text
    )
    assert text.splitlines()[-1] == (
        "templates check: 3 template(s), 1 checked, 2 without a stored definition, 0 drift(s), "
        "3 without a browse node"
    )


def test_check_uses_the_definition_of_the_templates_own_product_type() -> None:
    """A KURTA template is never checked against another type's definition."""
    checks = check_templates(_amazon_seed(), {"SHIRT": StoredDefinition("v2", _schema())})
    by_type = {one.product_type: one for one in checks}
    assert not by_type["KURTA"].checked
    assert by_type["SHIRT"].version == "v2"


def test_check_lists_drift_and_browse_nodes() -> None:
    drifted = _schema("KURTA.drift.schema.json")
    with_nodes = _declaring(drifted, ["111"])
    (one,) = check_templates(
        [("KURTA", _kurta(None)[0])], {"KURTA": StoredDefinition("v3", with_nodes)}
    )
    assert {(d.kind, d.attribute) for d in one.drifts} == {
        ("unknown_attribute", "neck_style"),
        ("unfed_required", "item_type_keyword"),
    }
    text = render_checks([one], list_nodes=True)
    assert "browse node not set (the definition lists 1)" in text
    assert "  browse node 111 Node 111" in text
    assert "  unknown_attribute neck_style: neckline -> not declared by KURTA" in text
    assert "browse node 111" not in render_checks([one])
