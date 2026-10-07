"""Brand document stubs (owner step S16)."""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import IdempotencyKey
from app.api.v1.schemas.brand import UploadChunkType
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["brand_documents_upload"])
@stub
async def brand_documents_upload(
    file: Annotated[UploadFile, File(description="The brand document (multipart, 10 MiB cap).")],
    idempotency_key: IdempotencyKey,
    type: Annotated[
        UploadChunkType,
        Form(description="The chunk type of every unit in the file (default `tone_rule`)."),
    ] = "tone_rule",
) -> NoReturn:
    """Upload a brand document (`.txt` or `.md`, UTF-8); a job chunks and embeds it."""
    raise NotImplementedYet("brand_documents_upload")


@route(router, OPS["brand_documents_list"])
@stub
async def brand_documents_list(page: Annotated[PageParams, Depends()]) -> NoReturn:
    """The tenant's live brand documents, newest first."""
    raise NotImplementedYet("brand_documents_list")


@route(router, OPS["brand_documents_delete"])
@stub
async def brand_documents_delete(document_id: UUID) -> NoReturn:
    """Delete a brand document; its chunks stop being used at once."""
    raise NotImplementedYet("brand_documents_delete")
