"""Bulk import models (owner step S13). The report and row shapes are `mkos_schemas.imports`."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, WireModel
from mkos_schemas.imports import ImportRowResult

from app.api.pagination import Page

ImportStatus = Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
ImportRowStatus = Literal["imported", "failed"]


class ImportAccepted(WireModel):
    import_id: UUID
    job_id: UUID
    status: ImportStatus = "queued"
    poll_url: NonBlankStr


class ImportRowPage(Page[ImportRowResult]):
    pass
