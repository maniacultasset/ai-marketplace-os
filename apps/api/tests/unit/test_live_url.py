"""S26 T21 (DATABASE.md:757: `live_url` is built by the adapter, https only, at most 2048
characters): `app.adapters.registry.live_url`."""

from __future__ import annotations

import pytest
from app.adapters.registry import live_url


def test_live_url_amazon_detail_page() -> None:
    assert live_url("amazon", "B0476B5518") == "https://www.amazon.in/dp/B0476B5518"


@pytest.mark.parametrize("external_id", ["a b", "x/../y", "q?x=1", "ü"])
def test_live_url_amazon_quotes_the_id(external_id: str) -> None:
    url = live_url("amazon", external_id)
    assert url is not None and url.startswith("https://www.amazon.in/dp/")
    tail = url.removeprefix("https://www.amazon.in/dp/")
    assert all(c.isalnum() or c in "%-._~" for c in tail), url


@pytest.mark.parametrize("external_id", [None, "", "   ", "x" * 2049])
def test_live_url_none_without_a_usable_id(external_id: str | None) -> None:
    assert live_url("amazon", external_id) is None


def test_live_url_flipkart_unknown() -> None:
    """A Flipkart product page needs a slug and the FSN the listings API does not return
    (believed, DW1): no URL rather than a guessed one."""
    assert live_url("flipkart", "LSTABC123") is None
