"""Templated trail and question text for the fix pass (SI4 A4, A11; T54: no LLM text in v1).

No template ever formats a listing value: the trail names the slot, the fix and the sizes, so the
audit `reason` and the issue's `action_taken` never copy seller or model text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from mkos_schemas.enums import FixAction

ACTION_MIN: Final = 5
ACTION_MAX: Final = 1000

FIELD_LABELS: Final[Mapping[str, str]] = {
    "content.title": "title",
    "content.description": "description",
    "content.bullets": "bullet point",
    "keywords.search_terms": "search terms",
    "keywords.hidden_terms": "hidden search terms",
}
_UNITS: Final[Mapping[str, tuple[str, str]]] = {
    "chars": ("character", "characters"),
    "bytes": ("byte", "bytes"),
    "words": ("word", "words"),
    "values": ("value", "values"),
    "terms": ("term", "terms"),
}


def field_label(field: str) -> str:
    """A plain name for a field path: `content.bullets[2]` → "bullet point 3",
    `attributes.fit_type` → "fit type"."""
    if field.startswith("content.bullets[") and field.endswith("]"):
        index = field[len("content.bullets[") : -1]
        return f"bullet point {int(index) + 1}" if index.isdigit() else "bullet point"
    if field in FIELD_LABELS:
        return FIELD_LABELS[field]
    if field.startswith("attributes."):
        return field.removeprefix("attributes.").replace("_", " ")
    return field


def _unit(unit: str, count: int) -> str:
    singular, plural = _UNITS.get(unit, (unit, unit))
    return singular if count == 1 else plural


def _cap(text: str) -> str:
    return text[:ACTION_MAX]


def action_taken(
    fix_action: FixAction,
    field: str,
    *,
    before_size: int,
    after_size: int,
    unit: str,
    limit: int | None,
    duplicates: int = 0,
    trimmed: bool = True,
) -> str:
    """One sentence per fix action and slot family; 5..1000 characters; never a value."""
    label = field_label(field)
    if fix_action is FixAction.REMAP_VALUE:
        return _cap(
            f"Matched the {label} value to the spelling the marketplace accepts "
            f"({after_size} {_unit('values', after_size)} changed)."
        )
    if fix_action is not FixAction.REFORMAT:
        return _cap(f"Applied the {fix_action.value} fix to the {label}.")
    sizes = f"from {before_size} to {after_size} {_unit(unit, after_size)}"
    bound = "" if limit is None else f" to fit the {limit}-{_unit(unit, 1)} limit"
    if field.startswith("keywords."):
        parts: list[str] = []
        if duplicates:
            parts.append(f"removed {duplicates} repeated {_unit('terms', duplicates)}")
        if trimmed:
            parts.append("dropped whole terms from the end")
        done = " and ".join(parts) or "cleaned up spacing"
        return _cap(f"In the {label}, {done}, {sizes}{bound}.")
    if not trimmed:
        return _cap(f"Cleaned up spacing in the {label}, {sizes}{bound}.")
    return _cap(f"Shortened the {label} at a word boundary {sizes}{bound}.")
