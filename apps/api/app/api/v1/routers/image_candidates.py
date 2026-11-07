"""Image candidates (owner step SI7, ADR-0030 proposed): ask for a better main image or a
size-chart image, and list the product's candidates. Writes need `product:import` (owner,
operator); the list any member. Another tenant's product or image is 404, never 403. Routes
import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam

from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.image_candidates import (
    ImageCandidateAccepted,
    ImageCandidateIn,
    ImageCandidateOut,
    ImageCandidatePage,
)
from app.api.v1.schemas.images import ImageAccepted
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import image_candidates

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["image_candidates_create"])
async def image_candidates_create(
    product_id: Annotated[UUID, PathParam()],
    body: ImageCandidateIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ImageCandidateAccepted:
    """Ask for a candidate: `white_background` (an AI edit of the live main photo; only while
    image editing is switched on), `fix` (re-frame the main photo to a white square, no AI) or
    `size_chart` (an image of the product's size chart, no AI). A candidate is never a product
    image until it is accepted. The same source asked again answers the same candidate; 202 names
    its job only when a new one started (poll `poll_url`)."""
    answer = await image_candidates.create_candidate(
        principal,
        product_id,
        image_candidates.CandidateRequest(kind=body.kind, source_image_id=body.source_image_id),
        idempotency_key,
        request.url.path,
    )
    return ImageCandidateAccepted.model_validate(answer)


@route(router, OPS["image_candidates_list"])
async def image_candidates_list(
    product_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> ImageCandidatePage:
    """The product's candidates, newest first (at most 50). `preview_url` and
    `source_preview_url` are signed for 10 minutes: list again for fresh ones."""
    rows = await image_candidates.list_candidates(principal, product_id)
    return ImageCandidatePage(items=[ImageCandidateOut.model_validate(r) for r in rows])


@route(router, OPS["image_candidates_accept"])
async def image_candidates_accept(
    candidate_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ImageAccepted:
    """Use a `ready` candidate as a product image. A white-background or fix candidate becomes
    the main image and the old main stays as an additional image; a size-chart candidate
    replaces the size-chart image. The new image gets its own checks (poll `poll_url`). A
    candidate whose photo or size chart changed since it was made is 409."""
    body = await image_candidates.accept_candidate(
        principal, candidate_id, idempotency_key, request.url.path
    )
    return ImageAccepted.model_validate(body)


@route(router, OPS["image_candidates_reject"])
async def image_candidates_reject(
    candidate_id: Annotated[UUID, PathParam()],
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
) -> ImageCandidateOut:
    """Discard a `ready` candidate (asking for the same one again brings it back, at no cost).
    The answer carries no preview URLs: list the candidates again for fresh ones."""
    body = await image_candidates.reject_candidate(
        principal, candidate_id, idempotency_key, request.url.path
    )
    return ImageCandidateOut.model_validate(body)
