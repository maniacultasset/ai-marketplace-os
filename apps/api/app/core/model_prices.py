"""Model prices for `agent_runs.cost_usd` (S16 PLAN D5; DATABASE.md:983-997: USD, NUMERIC(12,6)).

`EMBEDDING_PRICE_USD_PER_MTOK` is the list price per million input tokens, checked on the build
day (2026-10-07). A model outside the table is refused at worker boot (`config._boot_errors`), so a
cost is never guessed. `is_usable_openai_key` is the one test of "a real key is set": a missing or
blank key and the `.env.example` placeholder (`sk-REPLACE_ME`) are not usable.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final

EMBEDDING_PRICE_USD_PER_MTOK: Final[Mapping[str, Decimal]] = MappingProxyType(
    {"text-embedding-3-small": Decimal("0.02")}
)
_MILLION: Final = Decimal(1_000_000)
_SIX_DP: Final = Decimal("0.000001")
_PLACEHOLDER: Final = "REPLACE_ME"


def embedding_cost_usd(model: str, tokens: int) -> Decimal:
    """The cost of `tokens` input tokens, quantized to 6 dp half-up. An unknown model raises
    `KeyError` (naming the model, which is configuration, not a secret)."""
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("tokens must be a non-negative int")
    price = EMBEDDING_PRICE_USD_PER_MTOK[model]
    return (price * Decimal(tokens) / _MILLION).quantize(_SIX_DP, rounding=ROUND_HALF_UP)


def is_usable_openai_key(key: str | None) -> bool:
    return key is not None and key.strip() != "" and _PLACEHOLDER not in key
