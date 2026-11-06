"""Fabric composition: parse a seller's text into fibres and percents (SI1 T2; AC2, PLAN D1, D9(a)).

Pure and stdlib only. The grammar: parts split on `,`, `/`, `;`, `+` or the word `and`; each part
is `<pct>% <fibre>` or `<fibre> <pct>%`. Percents are 1..100 with at most one decimal and sum to
exactly 100. A fibre is NFC-normalized with its whitespace collapsed, 1..60 characters, and unique
after casefolding. 1..10 parts. The stored `normalized` form is `[{"fibre", "percent"}]`
(DATABASE.md:1229), with the fibre casefolded.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

MAX_PARTS: Final = 10
MAX_FIBRE_CHARS: Final = 60
HUNDRED: Final = Decimal(100)

Reason = Literal["format", "percent", "sum", "duplicate", "blank", "count"]

_SPLIT: Final = re.compile(r"\s*(?:[,/;+]|\s+and\s+)\s*", re.IGNORECASE)
# ASCII digits only: `\d` would accept other scripts' digits
_PCT: Final = r"([0-9]{1,3}(?:\.[0-9])?)\s*%"
_FRONT: Final = re.compile(rf"{_PCT}\s*(.*)", re.DOTALL)
_BACK: Final = re.compile(rf"(.*?)\s*{_PCT}", re.DOTALL)
_WS: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Fibre:
    fibre: str
    percent: Decimal


class CompositionError(ValueError):
    """A refused composition; `reason` names the rule."""

    def __init__(self, reason: Reason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: Reason = reason


def _part(raw: str) -> tuple[str, Decimal]:
    if not raw.strip():
        raise CompositionError("blank", "an empty part")
    match = _FRONT.fullmatch(raw) or _BACK.fullmatch(raw)
    if match is None:
        raise CompositionError("format", "each part is '<pct>% <fibre>' or '<fibre> <pct>%'")
    if match.re is _FRONT:
        pct_text, name = match.group(1), match.group(2)
    else:
        name, pct_text = match.group(1), match.group(2)
    if "%" in name:
        raise CompositionError("format", "one percent per part")
    fibre = _WS.sub(" ", name).strip()
    if not fibre:
        raise CompositionError("blank", "a fibre name is empty")
    if len(fibre) > MAX_FIBRE_CHARS:
        raise CompositionError("format", f"a fibre name is over {MAX_FIBRE_CHARS} characters")
    percent = Decimal(pct_text)
    if not (Decimal(0) < percent <= HUNDRED):
        raise CompositionError("percent", "each percent is above 0 and at most 100")
    return fibre, percent


def parse_composition(text: str) -> tuple[Fibre, ...]:
    """Parse `text`, or raise `CompositionError`."""
    if "\x00" in text:
        raise CompositionError("format", "NUL")
    text = unicodedata.normalize("NFC", text)
    if not text.strip():
        raise CompositionError("blank", "empty")
    raw_parts = _SPLIT.split(text.strip())
    if len(raw_parts) > MAX_PARTS:
        raise CompositionError("count", f"at most {MAX_PARTS} fibres")
    fibres: list[Fibre] = []
    seen: set[str] = set()
    for raw in raw_parts:
        name, percent = _part(raw)
        key = name.casefold()
        if key in seen:
            raise CompositionError("duplicate", "a fibre is listed twice")
        seen.add(key)
        fibres.append(Fibre(fibre=name, percent=percent))
    if sum((f.percent for f in fibres), Decimal(0)) != HUNDRED:
        raise CompositionError("sum", "the percents must add up to 100")
    return tuple(fibres)


def normalized(fibres: Sequence[Fibre]) -> list[dict[str, object]]:
    """The stored form: `[{"fibre": <casefolded>, "percent": <int or one-decimal number>}]`."""
    out: list[dict[str, object]] = []
    for f in fibres:
        percent: object = (
            int(f.percent) if f.percent == f.percent.to_integral_value() else float(f.percent)
        )
        out.append({"fibre": f.fibre.casefold(), "percent": percent})
    return out
