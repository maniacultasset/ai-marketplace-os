"""V1.1: the size run's key and SKU rules (`app.services.size_run`)."""

from __future__ import annotations

import pytest
from app.services.size_run import size_key, size_sku


@pytest.mark.parametrize(
    ("label", "key"),
    [("M", "m"), ("3XL", "3xl"), ("Free Size", "free size"), ("28/30", "28/30"), (" L ", "l")],
)
def test_size_key_matches_the_import(label: str, key: str) -> None:
    assert size_key(label) == key


@pytest.mark.parametrize("label", ["", "-M", "M*", "x" * 33, "Größe"])
def test_size_key_refuses_what_a_cell_cannot_hold(label: str) -> None:
    assert size_key(label) is None


def test_size_sku_is_the_family_rule() -> None:
    assert size_sku("KORA-01", "xl") == "KORA-01-XL"
    assert size_sku("KORA-01", "Free  Size") == "KORA-01-FREE-SIZE"
    assert size_sku("K" * 60, "XXL") == "K" * 60 + "-XXL"  # 64 characters at most
    assert size_sku("K" * 61, "XXL") is None
    assert size_sku("KORA", "M]") is None


def test_only_a_seller_chart_makes_sizes() -> None:
    from types import SimpleNamespace

    from app.core.errors import InputError
    from app.services.size_run import _chart_sizes

    table = {"unit": "cm", "basis": "garment", "rows": [{"size": "S"}, {"size": "M"}]}
    seller = SimpleNamespace(
        attributes={"size_measurements": {"normalized": table, "source": "seller"}}
    )
    assert _chart_sizes(seller) == ["S", "M"]  # type: ignore[arg-type]
    for fact in ({"normalized": table, "source": "ai"}, {"normalized": table}, None):
        row = SimpleNamespace(attributes={"size_measurements": fact} if fact else {})
        with pytest.raises(InputError):
            _chart_sizes(row)  # type: ignore[arg-type]
