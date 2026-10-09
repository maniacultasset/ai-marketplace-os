"""Brand document models (owner step S16). The storage key and the content hash never leave
the server."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, WireModel
from mkos_schemas.errors import ErrorReport

from app.api.pagination import Page

# `example` chunks are approved-listing memory (T93, 1.1), so an upload never makes them (CP4).
UploadChunkType = Literal["tone_rule", "glossary", "compliance"]
DocumentStatus = Literal["uploaded", "processing", "ready", "failed"]


class BrandDocumentOut(WireModel):
    id: UUID
    filename: NonBlankStr
    status: DocumentStatus
    chunk_count: int
    error: ErrorReport | None = None
    created_at: datetime


class BrandDocumentPage(Page[BrandDocumentOut]):
    pass
