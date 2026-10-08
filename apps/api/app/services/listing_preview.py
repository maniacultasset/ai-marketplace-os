"""`preview_listing`: `GET /api/v1/listings/{listing_id}/preview` (S20 T19; AC15).

What the marketplace adapter would send for the listing's current version, per SKU, with the
adapter's mapping errors. It goes through the pure registry previewer (no HTTP client, no
marketplace account, no token), so it sends nothing (invariant 1) and gives no verdict (the
Rules Engine decides, invariant 2). One `tenant_uow` read: another tenant's listing is 404.

- No listing → 404; no current version, or `generating` → 409 LISTING_BUSY.
- A marketplace with no adapter yet (Flipkart until S22) → `MarketplaceNotSupported` (501).
- Images are opaque `image:<id>` references, never URLs.
"""

from __future__ import annotations

import uuid
from typing import Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ListingStatus, Marketplace
from mkos_schemas.publish import MarketplacePreview, PreviewItem

from app.adapters.registry import previewer
from app.core.errors import ConflictError, NotFoundError
from app.core.rbac import Principal
from app.repositories.uow import tenant_uow
from app.services.adapter_input import build_adapter_listing, canonical_listing

LISTING_BUSY_REASON: Final = "listing_busy"


class MarketplaceNotSupported(Exception):  # noqa: N818 - a 501, not a failure of the request
    """No adapter for this marketplace yet; the router answers 501 NOT_IMPLEMENTED."""


async def preview_listing(principal: Principal, listing_id: uuid.UUID) -> MarketplacePreview:
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        source = await uow.listings.preview_source(listing_id)
    if source is None:
        raise NotFoundError("listing_id")
    if source.version is None or source.status == ListingStatus.GENERATING.value:
        raise ConflictError(ErrorCode.LISTING_BUSY, LISTING_BUSY_REASON)
    try:
        preview = previewer(source.marketplace)
    except NotImplementedError:
        raise MarketplaceNotSupported(source.marketplace) from None
    body = canonical_listing(source)
    refs = {image.image_id: f"image:{image.image_id}" for image in body.images}
    adapter_listing = build_adapter_listing(
        source, image_refs=refs, idempotency_key=f"preview:{listing_id}"
    )
    result = preview(adapter_listing)
    return MarketplacePreview(
        listing_id=source.listing_id,
        version=source.version.version,
        marketplace=Marketplace(source.marketplace),
        product_type=result.product_type,
        items=[
            PreviewItem(
                product_id=item.product_id,
                seller_sku=item.seller_sku,
                item_role=item.item_role,
                payload=dict(item.payload),
            )
            for item in result.items
        ],
        report=result.report,
    )
