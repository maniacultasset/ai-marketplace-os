"""Believed Amazon wire names against a stored Product Type Definition (DW2 T6; AC12). Pure.

`diff(schema, templates, product_type)` lists, value-free:
- `unknown_attribute`: a name this adapter sends (content, keyword, image and variation names in
  `constants.py`, and each template spec's `marketplace_field`) that the schema's `properties`
  does not declare;
- `unknown_value`: a template target spelling (`allowed_values` and `value_map` targets) outside
  the attribute's `items.properties.value.enum`, when the schema has one;
- `unfed_required`: a name in the schema's top-level `required` that nothing here sends;
- `shape`: a spec attribute the schema declares without a `value` member (for example a
  structured `item_dimensions` of length, width and height) while the adapter sends one value.

Client item 5 (2026-11-12): a template that names a browse node (`external_category_id`) feeds
`recommended_browse_nodes`; the node must be digits, declared, and in the attribute's enum when
the schema lists one (`unknown_attribute` / `unknown_value`, value-free).

`templates` are the seed's Amazon templates for `product_type`; with none, only the constants
are checked. `render` prints one sorted line per drift.

SI8: the believed names include `brand`, `model_number` and `part_number` (every item sends
them); for an apparel product type (`c.AUDIENCE_PRODUCT_TYPES`) the audience names
(`audience_names()`) are checked as declared and counted as fed, and every `c.AUDIENCE` target is
checked against that attribute's enum. All of them stay believed until the real LISTING
definitions are stored and this diff runs on them (SI8 DL-A).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mkos_schemas.jobs import CategoryTemplate

from app.adapters.amazon import constants as c

DriftKind = Literal["unknown_attribute", "unknown_value", "unfed_required", "shape"]


@dataclass(frozen=True, order=True)
class Drift:
    kind: DriftKind
    attribute: str
    detail: str


def believed_names() -> tuple[str, ...]:
    """Every wire name the adapter sends whatever the template."""
    return (
        *c.CONTENT_FIELDS.values(),
        c.KEYWORD_ATTRIBUTE,
        c.MAIN_IMAGE,
        *c.OTHER_IMAGES,
        *c.VARIATION_ATTRIBUTES,
        c.BRAND_ATTRIBUTE,
        c.MODEL_NUMBER,
        c.PART_NUMBER,
    )


def audience_names() -> tuple[str, ...]:
    """SI8: the names an apparel item sends from its one `gender` fact."""
    return c.AUDIENCE_ATTRIBUTES


def sellable_names() -> tuple[str, ...]:
    """SI2: the names a sellable item sends from the seller's facts (the offer, the
    manufacturer contact, since SI5 the barcode or its exemption, and the packed box). They
    feed a `required` of the LISTING definition; a PRODUCT_ONLY definition need not declare
    them, so their absence is not drift."""
    return (
        c.OFFER_ATTRIBUTE,
        c.CONDITION_ATTRIBUTE,
        c.MANUFACTURER_CONTACT,
        c.EXTERNAL_ID_ATTRIBUTE,  # SI5 B2: the barcode or the exemption
        c.EXEMPTION_ATTRIBUTE,
        c.PACKAGE_DIMENSIONS,  # client item 15: the packed box
        c.PACKAGE_WEIGHT,
    )


def _item_properties(spec: Any) -> Mapping[str, Any] | None:
    items = spec.get("items") if isinstance(spec, dict) else None
    props = items.get("properties") if isinstance(items, dict) else None
    return props if isinstance(props, dict) else None


def _enum(spec: Any) -> list[Any] | None:
    props = _item_properties(spec)
    value = props.get("value") if props is not None else None
    enum = value.get("enum") if isinstance(value, dict) else None
    return enum if isinstance(enum, list) else None


def diff(
    schema: Mapping[str, Any], templates: Sequence[CategoryTemplate], product_type: str
) -> list[Drift]:
    raw = schema.get("properties")
    properties: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    found: set[Drift] = set()
    fed: set[str] = set(believed_names()) | set(sellable_names())
    for name in believed_names():
        if name not in properties:
            found.add(Drift("unknown_attribute", name, f"not declared by {product_type}"))
    if product_type in c.AUDIENCE_PRODUCT_TYPES:
        for name in audience_names():
            fed.add(name)
            if name not in properties:
                found.add(Drift("unknown_attribute", name, f"not declared by {product_type}"))
                continue
            enum = _enum(properties[name])
            if enum is None:
                continue
            targets = {audience[name] for audience in c.AUDIENCE.values()}
            for target in sorted(targets - {str(v) for v in enum}):
                found.add(
                    Drift(
                        "unknown_value",
                        name,
                        f"audience value {target} is not among the {len(enum)} allowed values",
                    )
                )
    for template in templates:
        node = (template.external_category_id or "").strip()
        if node:
            found |= _browse_node_drift(properties, node, template.category, product_type)
            fed.add(c.BROWSE_NODE_ATTRIBUTE)
        for key, spec in template.specs.items():
            if not spec.marketplace_field:
                continue
            name = spec.marketplace_field
            fed.add(name)
            if name not in properties:
                found.add(
                    Drift("unknown_attribute", name, f"{key} -> not declared by {product_type}")
                )
                continue
            declared = properties[name]
            members = _item_properties(declared)
            if members is not None and "value" not in members:
                shape = ", ".join(sorted(str(m) for m in members if m != "marketplace_id"))
                found.add(Drift("shape", name, f"{key} sends one value; the schema has {shape}"))
                continue
            enum = _enum(declared)
            if enum is None:
                continue
            targets = set(spec.allowed_values or []) | set(spec.value_map.values())
            for target in sorted(targets - {str(v) for v in enum}):
                found.add(
                    Drift(
                        "unknown_value",
                        name,
                        f"{key} value {target} is not among the {len(enum)} allowed values",
                    )
                )
    required = schema.get("required")
    for name in required if isinstance(required, list) else []:
        if isinstance(name, str) and name not in fed:
            found.add(Drift("unfed_required", name, "required, and nothing here sends it"))
    return sorted(found)


def _browse_node_drift(
    properties: Mapping[str, Any], node: str, category: str, product_type: str
) -> set[Drift]:
    """Client item 5: a template's browse node is a node id the schema accepts. Value-free: the
    category names the template, never the id."""
    name = c.BROWSE_NODE_ATTRIBUTE
    if not re.fullmatch(c.BROWSE_NODE_PATTERN, node):
        return {Drift("unknown_value", name, f"{category} browse node is not a node id")}
    if name not in properties:
        return {Drift("unknown_attribute", name, f"{category} -> not declared by {product_type}")}
    enum = _enum(properties[name])
    if enum is not None and node not in {str(v) for v in enum}:
        return {
            Drift(
                "unknown_value",
                name,
                f"{category} browse node is not among the {len(enum)} allowed values",
            )
        }
    return set()


def browse_node_choices(schema: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The browse nodes a stored definition offers, as (id, name) pairs sorted by id: its
    `recommended_browse_nodes` value `enum`, named by `enumNames` when given. Empty when the
    schema lists none (most do not)."""
    raw = schema.get("properties")
    properties: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    props = _item_properties(properties.get(c.BROWSE_NODE_ATTRIBUTE))
    value = props.get("value") if props is not None else None
    if not isinstance(value, dict) or not isinstance(value.get("enum"), list):
        return []
    names = value.get("enumNames")
    named = names if isinstance(names, list) and len(names) == len(value["enum"]) else None
    pairs = {
        str(v): (str(named[i]) if named is not None else "") for i, v in enumerate(value["enum"])
    }
    return sorted(pairs.items())


def render(drifts: Sequence[Drift]) -> str:
    return "\n".join(f"{d.kind} {d.attribute}: {d.detail}" for d in sorted(drifts))


# ---- client item 5 (2026-11-12): `templates check`, every Amazon category template at once ----


@dataclass(frozen=True)
class StoredDefinition:
    """The current stored definition of one product type: its version and schema JSON."""

    version: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class TemplateCheck:
    category: str
    product_type: str
    browse_node: str | None
    """The template's node id, None when not set."""
    version: str | None
    """The stored definition's version; None when none is stored (then nothing is checked)."""
    drifts: tuple[Drift, ...] = ()
    choices: tuple[tuple[str, str], ...] = ()
    """The browse nodes the stored definition lists, (id, name)."""

    @property
    def checked(self) -> bool:
        return self.version is not None


def check_templates(
    templates: Sequence[tuple[str, CategoryTemplate]],
    stored: Mapping[str, StoredDefinition | None],
) -> list[TemplateCheck]:
    """Each Amazon category template `(product_type, template)` against the stored definition of
    its own product type (`stored[product_type]`, None when none is stored). Pure."""
    out: list[TemplateCheck] = []
    for product_type, template in sorted(templates, key=lambda t: (t[1].category, t[0])):
        node = (template.external_category_id or "").strip() or None
        found = stored.get(product_type)
        if found is None:
            out.append(TemplateCheck(template.category, product_type, node, None))
            continue
        out.append(
            TemplateCheck(
                template.category,
                product_type,
                node,
                found.version,
                tuple(diff(found.schema, [template], product_type)),
                tuple(browse_node_choices(found.schema)),
            )
        )
    return out


def render_checks(checks: Sequence[TemplateCheck], *, list_nodes: bool = False) -> str:
    """One line per template, its drift lines indented under it, then a summary line. With
    `list_nodes`, the browse nodes each stored definition lists too (public taxonomy ids, the
    values a template's `external_category_id` takes)."""
    lines: list[str] = []
    for one in checks:
        node = "browse node set" if one.browse_node else "browse node not set"
        if one.choices:
            node += f" (the definition lists {len(one.choices)})"
        if one.version is None:
            state = f"no stored definition for {one.product_type}, not checked"
        else:
            state = f"definition {one.version}, {len(one.drifts)} drift(s)"
        lines.append(f"{one.category} -> {one.product_type}: {state}; {node}")
        lines += [f"  {d.kind} {d.attribute}: {d.detail}" for d in one.drifts]
        if list_nodes:
            lines += [f"  browse node {i} {name}".rstrip() for i, name in one.choices]
    unchecked = sum(1 for one in checks if not one.checked)
    drifts = sum(len(one.drifts) for one in checks)
    unset = sum(1 for one in checks if not one.browse_node)
    lines.append(
        f"templates check: {len(checks)} template(s), {len(checks) - unchecked} checked, "
        f"{unchecked} without a stored definition, {drifts} drift(s), "
        f"{unset} without a browse node"
    )
    return "\n".join(lines)
