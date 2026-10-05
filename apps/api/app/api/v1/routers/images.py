"""Image stubs (owner step S17b)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ImageSlot

from app.api.errors import ApiError, NotImplementedYet
from app.api.v1.deps import IdempotencyKey
from app.api.v1.schemas.common import StorageKey
from app.api.v1.schemas.images import UploadUrlRequest
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["images_upload_url"])
@stub
async def images_upload_url(body: UploadUrlRequest) -> NoReturn:
    """A short-lived signed URL to PUT one image to; its `storage_key` is then sent to
    `/images/upload`."""
    raise NotImplementedYet("images_upload_url")


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
