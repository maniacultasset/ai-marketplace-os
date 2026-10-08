"""The network-free previewers, by marketplace (S20 C1; G11).

Services may import this module and nothing else of an adapter: it reaches only the pure
mappings, never a client, a token or a socket (`.importlinter` `pure-mapping-no-io` and
`marketplace-io-only-in-adapters-and-workers`). Flipkart has no mapping until S22.
"""

from __future__ import annotations

from collections.abc import Callable

from mkos_schemas.enums import Marketplace

from app.adapters.amazon import mapping as amazon_mapping
from app.adapters.base import AdapterListing, AdapterPreview

Previewer = Callable[[AdapterListing], AdapterPreview]


def previewer(marketplace: Marketplace | str) -> Previewer:
    """The pure `preview` for `marketplace`; `NotImplementedError` for one with no adapter."""
    market = Marketplace(marketplace)
    if market is Marketplace.AMAZON:
        return amazon_mapping.preview
    raise NotImplementedError(f"no {market.value} adapter yet")
