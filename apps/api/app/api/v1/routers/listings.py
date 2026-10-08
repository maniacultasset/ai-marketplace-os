"""Listing routes: S20's preview; the rest are stubs (owner steps S18, S19, S21).

`GET /listings` is registered before `GET /listings/{listing_id}`; the batch ops
(`generate|validate|approve|publish`) are POST-only, so they never shadow a listing id.
"""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query
from fastapi import Path as PathParam
from mkos_schemas.enums import ListingStatus, Marketplace
from mkos_schemas.jobs import GenerateListingRequest
from mkos_schemas.publish import MarketplacePreview

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import IdempotencyKey, IfMatch, get_current_principal
from app.api.v1.schemas.batches import ApproveRequest, PublishRequest
from app.api.v1.schemas.listings import ListingContentPatch, RegenerateRequest, ValidateRequest
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import listing_preview

router = APIRouter()
ListingId = Annotated[UUID, PathParam()]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["listings_generate"])
@stub
async def listings_generate(
    body: GenerateListingRequest, idempotency_key: IdempotencyKey
) -> NoReturn:
    """Start AI generation for one product on one marketplace: 202 with the job."""
    raise NotImplementedYet("listings_generate")


@route(router, OPS["listings_validate"])
@stub
async def listings_validate(body: ValidateRequest) -> NoReturn:
    """Schema, rules and vision checks. A rules failure is 200 with `passed: false`, never 422;
    422 is only for a malformed request."""
    raise NotImplementedYet("listings_validate")


@route(router, OPS["listings_approve"])
@stub
async def listings_approve(body: ApproveRequest, idempotency_key: IdempotencyKey) -> NoReturn:
    """Approve or reject up to 200 listings. The approver is the JWT subject; whoever generated
    or edited a version cannot approve it (403 MAKER_CHECKER_VIOLATION per item)."""
    raise NotImplementedYet("listings_approve")


@route(router, OPS["listings_publish"])
@stub
async def listings_publish(body: PublishRequest, idempotency_key: IdempotencyKey) -> NoReturn:
    """Publish approved listings (owner or operator): 202 with per-item results."""
    raise NotImplementedYet("listings_publish")


@route(router, OPS["listings_list"])
@stub
async def listings_list(
    page: Annotated[PageParams, Depends()],
    status: Annotated[ListingStatus | None, Query()] = None,
    marketplace: Annotated[Marketplace | None, Query()] = None,
    product_id: Annotated[UUID | None, Query()] = None,
) -> NoReturn:
    """List the brand's listings (the review queue), filtered by status, marketplace or product."""
    raise NotImplementedYet("listings_list")


@route(router, OPS["listings_get"])
@stub
async def listings_get(listing_id: ListingId) -> NoReturn:
    """One listing with its current content and latest validation."""
    raise NotImplementedYet("listings_get")


@route(router, OPS["listings_preview"])
async def listings_preview(
    listing_id: ListingId, principal: CurrentPrincipal
) -> MarketplacePreview:
    """What the marketplace adapter would send for the current version, per SKU, with its
    mapping errors. Sends nothing and needs no marketplace account. 409 LISTING_BUSY while the
    listing has no version or is generating; 501 for a marketplace with no adapter yet."""
    try:
        return await listing_preview.preview_listing(principal, listing_id)
    except listing_preview.MarketplaceNotSupported:
        raise NotImplementedYet("listings_preview") from None


@route(router, OPS["listings_status"])
@stub
async def listings_status(listing_id: ListingId) -> NoReturn:
    """Status by listing id: a convenience alias; poll `/jobs/{job_id}` for progress."""
    raise NotImplementedYet("listings_status")


@route(router, OPS["listings_content_patch"])
@stub
async def listings_content_patch(
    listing_id: ListingId, body: ListingContentPatch, if_match: IfMatch
) -> NoReturn:
    """A human edit: creates a new version, and the editor can no longer approve it. `If-Match`
    names the version the edit is based on (409 STALE_VERSION when it is not current)."""
    raise NotImplementedYet("listings_content_patch")


@route(router, OPS["listings_regenerate"])
@stub
async def listings_regenerate(
    listing_id: ListingId,
    idempotency_key: IdempotencyKey,
    body: Annotated[RegenerateRequest | None, Body()] = None,
) -> NoReturn:
    """Regenerate an existing listing; a reviewer may do this, but may not generate fresh."""
    raise NotImplementedYet("listings_regenerate")


@route(router, OPS["listings_retry_fields"])
@stub
async def listings_retry_fields(listing_id: ListingId, idempotency_key: IdempotencyKey) -> NoReturn:
    """Retry only the fields that failed to publish (invariant 10)."""
    raise NotImplementedYet("listings_retry_fields")
