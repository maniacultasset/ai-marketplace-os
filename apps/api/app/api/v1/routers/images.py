"""Images (owner step S17b): the signed upload URL, the upload (a multipart `file` or a
`storage_key` from the signed step) and the report. Writes need `product:import` (owner,
operator); the report any member. Another tenant's product, key or image is 404, never 403.
Routes import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot, Marketplace

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.common import StorageKey
from app.api.v1.schemas.images import (
    ImageAccepted,
    ImageReportOut,
    ImageReports,
    UploadUrlOut,
    UploadUrlRequest,
)
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import images

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["images_upload_url"])
async def images_upload_url(body: UploadUrlRequest, principal: CurrentPrincipal) -> UploadUrlOut:
    """A short-lived signed URL to PUT one image to; its `storage_key` is then sent to
    `/images/upload`."""
    # The client sends every returned header with the PUT (`Content-Type`, `If-None-Match: *`;
    # the length is signed too): the URL creates the object once and never overwrites it.
    minted = await images.mint_upload_url(
        principal,
        product_id=body.product_id,
        slot=body.slot,
        content_type=body.content_type,
        size=body.bytes,
    )
    return UploadUrlOut(
        upload_url=minted.upload_url,
        storage_key=minted.storage_key,
        expires_at=minted.expires_at,
        headers=minted.headers,
    )


@route(router, OPS["images_upload"])
async def images_upload(
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    product_id: Annotated[UUID, Form()],
    slot: Annotated[ImageSlot, Form()],
    file: Annotated[UploadFile | None, File(description="The image (10 MiB cap).")] = None,
    storage_key: Annotated[
        StorageKey | None, Form(description="A key from `/images/upload-url`, instead of `file`.")
    ] = None,
    colourway_id: Annotated[UUID | None, Form()] = None,
) -> ImageAccepted:
    """Upload an image, or name one already uploaded, and start the compliance checks. Send
    exactly one of `file` and `storage_key`."""
    # A key is registered within 23 hours of its PUT (CP3); 202 names the image and its
    # `image_compliance` job (poll `poll_url`). The docstrings above are the frozen OpenAPI text.
    if (file is None) == (storage_key is None):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_FAILED,
            details=[{"loc": ["body", "file"], "msg": "Send exactly one of file or storage_key."}],
        )
    data = None if file is None else await file.read()
    form = images.ImageUploadForm(
        product_id=product_id, slot=slot, colourway_id=colourway_id, storage_key=storage_key
    )
    try:
        body = await images.accept_image_upload(principal, idempotency_key, form, data)
    except images.ImageTooLargeError:
        raise ApiError(413, ErrorCode.PAYLOAD_TOO_LARGE) from None
    return ImageAccepted.model_validate(body)


@route(router, OPS["images_report"])
async def images_report(
    image_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> ImageReportOut:
    """The image's compliance status and per-marketplace `ImageReport`s."""
    # `reports` is empty while pending; a replaced or unknown image is 404 (CP4).
    view = await images.get_report(principal, image_id)
    return ImageReportOut(
        image_id=view.image_id,
        compliance_status=view.compliance_status,
        reports=ImageReports(
            amazon=view.reports.get(Marketplace.AMAZON),
            flipkart=view.reports.get(Marketplace.FLIPKART),
        ),
    )
