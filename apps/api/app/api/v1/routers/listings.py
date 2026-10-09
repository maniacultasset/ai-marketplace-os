"""Listing routes: S18's generate, regenerate, status, content patch and validate; S20's preview;
S19's list, detail, history and approve; the rest are stubs (owner step S21).

`GET /listings` is registered before `GET /listings/{listing_id}`; the batch ops
(`generate|validate|approve|publish`) are POST-only, so they never shadow a listing id.
"""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ListingStatus, Marketplace
from mkos_schemas.jobs import GenerateListingRequest, JobAccepted
from mkos_schemas.publish import MarketplacePreview
from mkos_schemas.validation import ValidationSummary

from app.api.errors import ApiError, NotImplementedYet
from app.api.pagination import (
    PageInfo,
    PageParams,
    TaggedPageParams,
    decode_tagged_cursor,
    encode_cursor,
    encode_tagged_cursor,
)
from app.api.v1.deps import IdempotencyKey, IfMatch, get_current_principal
from app.api.v1.schemas.batches import ApproveRequest, BatchResult, PublishRequest
from app.api.v1.schemas.listings import (
    AuditEventOut,
    ListingContentPatch,
    ListingDetail,
    ListingHistoryPage,
    ListingOut,
    ListingPage,
    ListingStatusOut,
    RegenerateRequest,
    ValidateRequest,
)
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import (
    generation,
    listing_edit,
    listing_preview,
    listing_reads,
    listing_validate,
    review,
)
from app.services.budget import BudgetExceededError

router = APIRouter()
ListingId = Annotated[UUID, PathParam()]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def _budget(exc: BudgetExceededError) -> ApiError:
    return ApiError(
        429,
        ErrorCode.BUDGET_EXCEEDED,
        headers={"Retry-After": str(exc.retry_after_s)},
    )


@route(router, OPS["listings_generate"])
async def listings_generate(
    body: GenerateListingRequest,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> JobAccepted:
    """Start AI generation for one product on one marketplace: 202 with the job."""
    try:
        stored = await generation.accept_generate(
            principal, idempotency_key, request.url.path, body
        )
    except BudgetExceededError as exc:
        raise _budget(exc) from None
    return JobAccepted.model_validate(stored)


@route(router, OPS["listings_validate"])
async def listings_validate(
    body: ValidateRequest, principal: CurrentPrincipal
) -> ValidationSummary:
    """Schema, rules and vision checks. A rules failure is 200 with `passed: false`, never 422;
    422 is only for a malformed request."""
    return await listing_validate.validate(principal, body.listing_id, body.listing_version_id)


@route(router, OPS["listings_approve"])
async def listings_approve(
    body: ApproveRequest,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> BatchResult:
    """Approve or reject up to 200 listings. The approver is the JWT subject; whoever generated
    or edited a version cannot approve it (403 MAKER_CHECKER_VIOLATION per item). The request is
    200 with each item's own `http_status` and `error`."""
    for index, i in enumerate(body.items):
        if i.regenerate:  # the feedback becomes the regeneration's instructions (CP12 check)
            generation.check_instructions(i.feedback, f"items[{index}].feedback")
    items = [
        review.ReviewItem(
            listing_id=i.listing_id,
            version_id=i.listing_version_id,
            decision=i.decision,
            reason_code=i.reason_code,
            feedback=i.feedback,
            regenerate=i.regenerate,
        )
        for i in body.items
    ]
    stored = await review.accept_review(
        principal, idempotency_key, request.url.path, body.model_dump_json().encode(), items
    )
    return BatchResult.model_validate(stored)


@route(router, OPS["listings_publish"])
@stub
async def listings_publish(body: PublishRequest, idempotency_key: IdempotencyKey) -> NoReturn:
    """Publish approved listings (owner or operator): 202 with per-item results."""
    raise NotImplementedYet("listings_publish")


@route(router, OPS["listings_list"])
async def listings_list(
    page: Annotated[TaggedPageParams, Depends()],
    principal: CurrentPrincipal,
    status: Annotated[ListingStatus | None, Query()] = None,
    marketplace: Annotated[Marketplace | None, Query()] = None,
    product_id: Annotated[UUID | None, Query()] = None,
) -> ListingPage:
    """List the brand's listings, filtered by status, marketplace or product. `status=validated`
    or `needs_review` is the review queue, oldest first; any other filter is newest first."""
    wanted = None if status is None else ListingStatus(status).value
    tag = listing_reads.order_tag(wanted)
    after = None if page.cursor is None else decode_tagged_cursor(page.cursor, tag)
    found = await listing_reads.list_listings(
        principal,
        status=wanted,
        marketplace=None if marketplace is None else Marketplace(marketplace).value,
        product_id=product_id,
        after=after,
        limit=page.limit,
    )
    key = found.next_key
    return ListingPage(
        data=[ListingOut.model_validate(item) for item in found.items],
        page=PageInfo(
            next_cursor=None if key is None else encode_tagged_cursor(found.tag, *key),
            has_more=found.has_more,
            limit=page.limit,
        ),
    )


@route(router, OPS["listings_get"])
async def listings_get(
    listing_id: ListingId, principal: CurrentPrincipal, response: Response
) -> ListingDetail:
    """One listing with its current content and latest validation. `ETag` is the current
    version id: send it as `If-Match` on the content PATCH."""
    body, etag = await listing_reads.detail(principal, listing_id)
    if etag is not None:
        response.headers["ETag"] = etag
    return ListingDetail.model_validate(body)


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
async def listings_status(listing_id: ListingId, principal: CurrentPrincipal) -> ListingStatusOut:
    """Status by listing id: a convenience alias; poll `/jobs/{job_id}` for progress."""
    listing = await generation.read_status(principal, listing_id)
    return ListingStatusOut(
        listing_id=listing.id,
        status=listing.status,
        status_reason=listing.status_reason,
        marketplace_state=listing.marketplace_state,
        status_changed_at=listing.status_changed_at,
    )


@route(router, OPS["listings_history"])
async def listings_history(
    listing_id: ListingId, page: Annotated[PageParams, Depends()], principal: CurrentPrincipal
) -> ListingHistoryPage:
    """The listing's audit history (approvals, denials, rejections, edits), newest first."""
    found = await listing_reads.history(principal, listing_id, after=page.after, limit=page.limit)
    key = found.next_key
    return ListingHistoryPage(
        data=[AuditEventOut.model_validate(item) for item in found.items],
        page=PageInfo(
            next_cursor=None if key is None else encode_cursor(*key),
            has_more=found.has_more,
            limit=page.limit,
        ),
    )


@route(router, OPS["listings_content_patch"])
async def listings_content_patch(
    listing_id: ListingId,
    body: ListingContentPatch,
    if_match: IfMatch,
    principal: CurrentPrincipal,
    response: Response,
) -> ListingDetail:
    """A human edit: creates a new version, and the editor can no longer approve it. `If-Match`
    names the version the edit is based on (409 STALE_VERSION when it is not current)."""
    try:
        based_on = UUID(if_match.strip('"'))
    except ValueError:  # the header pattern admits 36 hex-or-dash characters that are no UUID
        raise ApiError(422, ErrorCode.VALIDATION_FAILED, "If-Match is not a version id.") from None
    detail = await listing_edit.edit(
        principal,
        listing_id,
        based_on,
        content=body.content,
        keywords=body.keywords,
        attributes=body.attributes,
    )
    etag = listing_reads.etag_of(detail.get("current_version_id"))
    if etag is not None:
        response.headers["ETag"] = etag
    return ListingDetail.model_validate(detail)


@route(router, OPS["listings_regenerate"])
async def listings_regenerate(
    listing_id: ListingId,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    body: Annotated[RegenerateRequest | None, Body()] = None,
) -> JobAccepted:
    """Regenerate an existing listing; a reviewer may do this, but may not generate fresh."""
    instructions = None if body is None else body.instructions
    try:
        stored = await generation.accept_regenerate(
            principal, idempotency_key, request.url.path, listing_id, instructions
        )
    except BudgetExceededError as exc:
        raise _budget(exc) from None
    return JobAccepted.model_validate(stored)


@route(router, OPS["listings_retry_fields"])
@stub
async def listings_retry_fields(listing_id: ListingId, idempotency_key: IdempotencyKey) -> NoReturn:
    """Retry only the fields that failed to publish (invariant 10)."""
    raise NotImplementedYet("listings_retry_fields")
