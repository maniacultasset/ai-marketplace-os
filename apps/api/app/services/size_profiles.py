"""Client feedback 17 Nov (P1-A): the size step a category asks for.

The profiles are data, `services/data/size_profiles.json`: per category the size system (`alpha`
letters or `waist` inches), the sizes offered in that order, and the size chart measures the
seller fills, in display order. A category without a profile (lamps, an unknown or no category)
has no size step: `profile_for` answers None. Tops and kurta sets keep the web form's old
defaults (XS-3XL; chest, waist, hip, length), so they read exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

__all__ = ["DATA_PATH", "MEASURES", "SizeProfile", "load", "parse", "profile_for"]

DATA_PATH: Final = Path(__file__).resolve().parent / "data" / "size_profiles.json"
MEASURES: Final = ("chest", "waist", "hip", "length", "shoulder", "sleeve", "inseam")
SizeSystem = Literal["alpha", "waist"]
_SYSTEMS: Final = ("alpha", "waist")


@dataclass(frozen=True, slots=True)
class SizeProfile:
    size_system: SizeSystem
    sizes: tuple[str, ...]
    measures: tuple[str, ...]


def _profile(slug: str, raw: dict[str, Any]) -> SizeProfile:
    system = raw["size_system"]
    sizes = tuple(str(s) for s in raw["sizes"])
    measures = tuple(raw["measures"])
    if system not in _SYSTEMS:
        raise ValueError(f"{slug}: unknown size system {system!r}")
    if not sizes or len(set(sizes)) != len(sizes):
        raise ValueError(f"{slug}: sizes must be non-empty and unique")
    if not measures or len(set(measures)) != len(measures) or not set(measures) <= set(MEASURES):
        raise ValueError(f"{slug}: measures must be distinct keys of {MEASURES}")
    return SizeProfile(size_system=system, sizes=sizes, measures=measures)


def parse(data: dict[str, Any]) -> dict[str, SizeProfile]:
    """The file's profiles by category; a malformed one raises `ValueError`."""
    return {slug: _profile(slug, raw) for slug, raw in data["profiles"].items()}


@lru_cache(maxsize=1)
def load() -> dict[str, SizeProfile]:
    return parse(json.loads(DATA_PATH.read_text(encoding="utf-8")))


def profile_for(category: str | None) -> SizeProfile | None:
    """The category's size profile, or None when it has no size step."""
    return load().get(category) if category else None
