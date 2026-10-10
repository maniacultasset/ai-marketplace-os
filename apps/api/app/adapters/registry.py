"""The network-free previewers, by marketplace (S20 C1; G11; S22 C9).

Services may import this module and nothing else of an adapter: it reaches only the pure
mappings, never a client, a token or a socket (`.importlinter` `pure-mapping-no-io` and
`marketplace-io-only-in-adapters-and-workers`).

- `previewer(marketplace)`: the pure `preview` of that marketplace's adapter.
- `family_has_parent(marketplace)`: whether a family sends a parent row (Amazon) or its children
  only (Flipkart: DATABASE.md:471, :754-755, :848; T45). The shared input builder
  (`app.services.adapter_input.build_adapter_listing`) reads it, so preview, publish and the
  item ledger see the same items.
"""

from __future__ import annotations

from collections.abc import Callable

from mkos_schemas.enums import Marketplace

from app.adapters.amazon import mapping as amazon_mapping
from app.adapters.base import AdapterListing, AdapterPreview
from app.adapters.flipkart import mapping as flipkart_mapping

Previewer = Callable[[AdapterListing], AdapterPreview]


def previewer(marketplace: Marketplace | str) -> Previewer:
    """The pure `preview` for `marketplace`."""
    market = Marketplace(marketplace)
    if market is Marketplace.AMAZON:
        return amazon_mapping.preview
    if market is Marketplace.FLIPKART:
        return flipkart_mapping.preview
    raise NotImplementedError(f"no {market.value} adapter yet")


def family_has_parent(marketplace: Marketplace | str) -> bool:
    return Marketplace(marketplace) is not Marketplace.FLIPKART


def live_url(marketplace: Marketplace | str, external_id: str | None) -> str | None:
    """The public product page of a live item, built by its adapter (DATABASE.md:757); None when
    the adapter cannot build one. Flipkart: a product page needs a slug and the FSN, which the
    listings API does not return (believed, DW1), so no URL is guessed."""
    if Marketplace(marketplace) is Marketplace.AMAZON:
        return amazon_mapping.live_url(external_id)
    return None
