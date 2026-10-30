from decimal import ROUND_HALF_UP, Decimal

# (provider, model) -> (input, output, cached_input) USD per million tokens.
# Unknown models fail tests rather than silently costing zero.
PRICES: dict[tuple[str, str], tuple[Decimal, Decimal, Decimal]] = {
    ("openai", "gpt-4o-mini"): (Decimal("0.15"), Decimal("0.60"), Decimal("0.075")),
    ("openai", "gpt-4.1-mini"): (Decimal("0.40"), Decimal("1.60"), Decimal("0.10")),
    ("openai", "gpt-4.1"): (Decimal("2.00"), Decimal("8.00"), Decimal("0.50")),
    ("deepseek", "deepseek-chat"): (Decimal("0.27"), Decimal("1.10"), Decimal("0.07")),
    ("deepseek", "deepseek/deepseek-chat"): (Decimal("0.27"), Decimal("1.10"), Decimal("0.07")),
    ("deepseek", "deepseek-v3.2"): (Decimal("0.27"), Decimal("1.10"), Decimal("0.07")),
    ("deepseek", "deepseek-reasoner"): (Decimal("0.55"), Decimal("2.19"), Decimal("0.14")),
}

_QUANTUM = Decimal("0.000001")


class UnknownModelPricing(ValueError):
    """Raised when (provider, model) is missing from the price table."""


def quantize_cost(value: Decimal | float | str) -> str:
    quantized = Decimal(str(value)).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{quantized:.6f}"


def compute_cost(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> str:
    key = (provider, model)
    if key not in PRICES:
        raise UnknownModelPricing(f"No price for provider={provider!r} model={model!r}")
    input_rate, output_rate, cached_rate = PRICES[key]
    million = Decimal("1000000")
    billed_input = max(input_tokens - cached_input_tokens, 0)
    total = (
        Decimal(billed_input) * input_rate / million
        + Decimal(cached_input_tokens) * cached_rate / million
        + Decimal(output_tokens) * output_rate / million
    )
    return quantize_cost(total)
