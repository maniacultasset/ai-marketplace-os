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

`templates` are the seed's Amazon templates for `product_type`; with none, only the constants
are checked. `render` prints one sorted line per drift.
"""

from __future__ import annotations

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
    fed: set[str] = set(believed_names())
    for name in believed_names():
        if name not in properties:
            found.add(Drift("unknown_attribute", name, f"not declared by {product_type}"))
    for template in templates:
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


def render(drifts: Sequence[Drift]) -> str:
    return "\n".join(f"{d.kind} {d.attribute}: {d.detail}" for d in sorted(drifts))
