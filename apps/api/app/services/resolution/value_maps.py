"""Value-map proposals from outcomes (SI13 T15; P:B4, D7; CP-D12). Pure: no I/O.

An observation is one closed marketplace `VALUE_NOT_ALLOWED` refusal of `attributes.<key>` on a
listing that later went live: the value refused (`v`) and the value the published version sent
(`w`, listing level only; a child's refusal has no live counterpart and is counted unpaired).
`propose` suggests an alias `fold(v) -> w` only when all of these hold:
- at least `min_listings` listings (3) went from `v` to `w`, with no listing going live with
  another `w` (a split is no rule);
- `w` is a value of the stored marketplace enum for the key;
- `fold(v)` is not already an alias key, and the key is on the allowlist (theme axes and the
  template's enum keys);
- `v` looks like an enum value: at most 64 characters, no control or bidi characters.
It never writes: approval is a reviewed PR to `adapters/amazon/aliases.py` that bumps
`ALIASES_VERSION`.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

VALUE_MAX: Final = 64
MIN_LISTINGS: Final = 3
AXIS_KEYS: Final = frozenset({"size", "color"})


def fold(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


@dataclass(frozen=True, slots=True)
class Observation:
    marketplace: str
    product_type: str | None
    key: str
    v: str
    w: str | None
    child: bool
    listing_id: str
    tenant_id: str


@dataclass(frozen=True, slots=True)
class ValueMapProposal:
    marketplace: str
    product_type: str | None
    key: str
    v: str
    w: str
    listings: int
    tenants: int

    def alias_line(self) -> str:
        return f'"{fold(self.v)}": ("{self.w}",),'


@dataclass(frozen=True, slots=True)
class UnpairedCount:
    marketplace: str
    product_type: str | None
    key: str
    v: str
    listings: int


def _clean(value: str) -> bool:
    if not value or len(value) > VALUE_MAX:
        return False
    return not any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value)


def propose(
    observations: Iterable[Observation],
    enums: Mapping[str, Sequence[str]],
    aliases: Mapping[str, object],
    *,
    allowlist: Iterable[str] = (),
    min_listings: int = MIN_LISTINGS,
) -> tuple[list[ValueMapProposal], list[UnpairedCount]]:
    allowed = AXIS_KEYS | set(allowlist)
    known = {fold(k) for k in aliases}
    pairs: dict[tuple[str, str | None, str, str], dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    tenants: dict[tuple[str, str | None, str, str], set[str]] = defaultdict(set)
    unpaired: dict[tuple[str, str | None, str, str], set[str]] = defaultdict(set)
    shown: dict[tuple[str, str | None, str, str], str] = {}
    for o in observations:
        if o.key not in allowed or not _clean(o.v):
            continue
        group = (o.marketplace, o.product_type, o.key, fold(o.v))
        shown.setdefault(group, o.v)
        if o.child or o.w is None:
            unpaired[group].add(o.listing_id)
            continue
        if not _clean(o.w):
            continue
        pairs[group][o.w].add(o.listing_id)
        tenants[group].add(o.tenant_id)
    found: list[ValueMapProposal] = []
    for group, targets in sorted(pairs.items(), key=lambda kv: kv[0]):
        if len(targets) != 1:
            continue  # a split: listings went live with different values
        ((w, listings),) = targets.items()
        marketplace, product_type, key, folded = group
        if len(listings) < min_listings or folded in known:
            continue
        if w not in set(enums.get(key, ())):
            continue
        found.append(
            ValueMapProposal(
                marketplace, product_type, key, shown[group], w, len(listings), len(tenants[group])
            )
        )
    counts = [
        UnpairedCount(g[0], g[1], g[2], shown[g], len(ids)) for g, ids in sorted(unpaired.items())
    ]
    return found, counts
