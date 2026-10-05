"""Brand document stubs (owner step S16)."""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, File, UploadFile

from app.api.errors import NotImplementedYet
from app.api.v1.deps import IdempotencyKey
from app.api.v1.surface import OPS, route, stub

router = APIRouter()


@route(router, OPS["brand_documents_upload"])
@stub
async def brand_documents_upload(
    file: Annotated[UploadFile, File(description="The brand document (multipart, 10 MiB cap).")],
    idempotency_key: IdempotencyKey,
) -> NoReturn:
    """Upload a brand document; a job chunks and embeds it."""
    raise NotImplementedYet("brand_documents_upload")
