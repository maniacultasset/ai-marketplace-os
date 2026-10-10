"""The submission ledger read (S21 D3): `GET /api/v1/listings/{listing_id}/submissions`, S3's keyset
envelope over `mkos_schemas.publish.SubmissionOut`, newest first by `(created_at, id)`."""

from __future__ import annotations

from mkos_schemas.publish import SubmissionOut

from app.api.pagination import Page


class SubmissionPage(Page[SubmissionOut]):
    pass
