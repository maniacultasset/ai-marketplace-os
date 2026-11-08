"""Reviewed synonym tables for `enum_fold` (SI11 T9; P:B2, D9; spec §21 "XXL -> 2XL"). Pure data.

A table maps a folded seller value to the marketplace spellings it may mean. A repair uses an
alias only when exactly one of its targets is in the attribute's marketplace enum, and records
`ALIASES_VERSION` with the repair. A change here is a reviewed PR that bumps the version.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

ALIASES_VERSION: Final = 1

SIZE_ALIASES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "xxl": ("2XL",),
        "2xl": ("XXL",),
        "xxxl": ("3XL",),
        "3xl": ("XXXL",),
        "xxxxl": ("4XL",),
        "4xl": ("XXXXL",),
        "xs": ("X-Small",),
        "small": ("S",),
        "medium": ("M",),
        "large": ("L",),
        "extra large": ("XL",),
        "free size": ("Free Size", "One Size"),
    }
)
# Applied only to an enum-valued colour attribute (the marketplace gives the enum); a free-text
# colour is never aliased.
COLOUR_FAMILY_ALIASES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "jet black": ("Black",),
        "off-white": ("Off White",),
        "navy blue": ("Navy",),
        "wine": ("Maroon", "Burgundy"),
        "multicolour": ("Multicolor",),
        "grey": ("Gray",),
        "gray": ("Grey",),
    }
)
SIZE_ATTRIBUTES: Final = frozenset({"size", "size_name", "apparel_size"})
COLOUR_ATTRIBUTES: Final = frozenset({"color", "color_name"})
