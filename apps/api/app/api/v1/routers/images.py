"""Images (owner step S17b): the signed upload URL, the upload (a multipart `file` or a
`storage_key` from the signed step) and the report. Writes need `product:import` (owner,
operator); the report any member. Another tenant's product, key or image is 404, never 403.
Routes import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot

from app.api.errors import ApiError, NotImplementedYet
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.common import StorageKey
from app.api.v1.schemas.images import UploadUrlOut, UploadUrlRequest
from app.api.v1.surface import OPS, route, stub
from app.core.rbac import Principal
from app.services import images

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@route(router, OPS["images_upload_url"])
async def images_upload_url(body: UploadUrlRequest, principal: CurrentPrincipal) -> UploadUrlOut:
    """A short-lived signed URL to PUT one image to; its `storage_key` is then sent to
    `/images/upload`. Send every returned header with the PUT (`Content-Type` and
    `If-None-Match: *`; the length is signed too): the URL can create the object once and
    never overwrite it."""
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
@stub
async def images_upload(
    idempotency_key: IdempotencyKey,
    product_id: Annotated[UUID, Form()],
    slot: Annotated[ImageSlot, Form()],
    file: Annotated[UploadFile | None, File(description="The image (10 MiB cap).")] = None,
    storage_key: Annotated[
        StorageKey | None, Form(description="A key from `/images/upload-url`, instead of `file`.")
    ] = None,
    colourway_id: Annotated[UUID | None, Form()] = None,
) -> NoReturn:
    """Upload an image, or name one already uploaded, and start the compliance checks. Send
    exactly one of `file` and `storage_key`."""
    if (file is None) == (storage_key is None):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_FAILED,
            details=[{"loc": ["body", "file"], "msg": "Send exactly one of file or storage_key."}],
        )
    raise NotImplementedYet("images_upload")


@route(router, OPS["images_report"])
@stub
async def images_report(image_id: Annotated[UUID, PathParam()]) -> NoReturn:
    """The image's compliance status and per-marketplace `ImageReport`s."""
    raise NotImplementedYet("images_report")
