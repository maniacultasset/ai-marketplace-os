"""SI2 T12: the resolved family facts `build_adapter_listing` now takes, for tests that build an
`AdapterListing` from a `PreviewSource` by hand."""

from __future__ import annotations

import uuid
from typing import Any

_FACTS: dict[uuid.UUID, Any] = {}


async def source_with_facts(tenant_id: uuid.UUID, listing_id: uuid.UUID) -> Any:
    """`preview_source(listing_id)` under `tenant_id`, remembering the product's family facts
    for `facts_of` (None when the listing is not visible)."""
    from app.repositories.uow import tenant_uow

    async with tenant_uow(tenant_id) as uow:
        source = await uow.listings.preview_source(listing_id)
        if source is not None:
            _FACTS[source.listing_id] = await uow.product_facts.resolve_family(source.product.id)
    return source


def facts_of(source: Any) -> Any:
    """The family facts `source_with_facts` read for this source."""
    found = _FACTS[source.listing_id]
    assert found is not None, "the listing's product does not resolve"
    return found
