"""Field-path vocabulary for errors, rules and publish results."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .listing import CanonicalListing

MAX_PATH_LENGTH: Final = 192

RULE_FIELD_EXTRAS: Final = ("*", "content.bullets[*]", "images")

FIELD_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^listing$",
    r"^request$",
    r"^file\.header$",
    r"^file\.row$",
    r"^content\.title$",
    r"^content\.bullets$",
    r"^content\.bullets\[[0-9]{1,3}\]$",
    r"^content\.description$",
    r"^keywords\.search_terms(\[[0-9]{1,3}\])?$",
    r"^keywords\.hidden_terms(\[[0-9]{1,3}\])?$",
    r"^attributes$",
    r"^attributes\.[a-z0-9_]{1,64}$",
    r"^images\[[0-9]{1,3}\]$",
    r"^facts\.[a-z][a-z0-9_]{1,63}$",
    r"^variants$",
    r"^variants\[[^\]\s]{1,64}\]$",
    r"^variants\[[^\]\s]{1,64}\]\.facts\.[a-z][a-z0-9_]{1,63}$",
    r"^brand\.[a-z][a-z0-9_]{1,63}$",
)

_COMPILED = tuple(re.compile(p) for p in FIELD_PATH_PATTERNS)
_SEGMENT = re.compile(r"^([a-z_][a-z0-9_]*)(?:\[([0-9]{1,3})\])?$")
_CONDITION_PRODUCT = re.compile(r"^product\.[a-z0-9_]{1,64}$")
_CONDITION_ACCOUNT = re.compile(r"^account\.[a-z0-9_]+(?:\.[a-z0-9_]+)*$")


def is_valid_field_path(path: str) -> bool:
    if len(path) > MAX_PATH_LENGTH:
        return False
    return any(p.fullmatch(path) for p in _COMPILED)


def is_valid_rule_field(field: str) -> bool:
    if field in RULE_FIELD_EXTRAS:
        return True
    return is_valid_field_path(field)


def is_valid_condition_path(path: str) -> bool:
    if _CONDITION_PRODUCT.fullmatch(path):
        return True
    if _CONDITION_ACCOUNT.fullmatch(path):
        return True
    if path.startswith(("facts.", "variants", "brand.", "request", "file.")):
        return False
    return is_valid_field_path(path)


def resolve(listing: CanonicalListing, path: str) -> object:
    if path in ("request", "file.header", "file.row") or path.startswith(
        ("facts.", "variants", "brand.")
    ):
        raise ValueError(f"non-listing root in path {path!r}")
    if path == "listing":
        return listing
    node: object = listing
    for part in path.split("."):
        m = _SEGMENT.match(part)
        if not m:
            raise ValueError(f"illegal field path segment: {part!r}")
        key, idx = m.group(1), m.group(2)
        if isinstance(node, dict):
            if key not in node:
                raise KeyError(key)
            node = node[key]
        else:
            node = getattr(node, key)
        if idx is not None:
            if not isinstance(node, list):
                raise TypeError("expected list segment")
            index = int(idx)
            if index >= len(node):
                raise IndexError(index)
            node = node[index]
    return node
