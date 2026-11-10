"""Product delete models: the bulk body and its per-product answers (`POST /products/bulk-delete`).

The answer has the batch shape of `listings_approve` (200 with each item's own `http_status` and
`ErrorReport`), without a `batch_id`: a delete opens no batch row.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from mkos_schemas.base import StrictModel, WireModel
from mkos_schemas.errors import ErrorReport
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

MAX_BULK_DELETE: Final = 100


class ProductBulkDeleteRequest(StrictModel):
    product_ids: Annotated[
        list[UUID],
        Field(
            min_length=1,
            max_length=MAX_BULK_DELETE,
            description="Up to 100 product ids, each once. A style takes its sizes with it.",
        ),
    ]

    @model_validator(mode="after")
    def _unique(self) -> ProductBulkDeleteRequest:
        if len(set(self.product_ids)) != len(self.product_ids):
            raise PydanticCustomError("mkos_invalid", "Each product_id may appear once.")
        return self


class ProductDeleteItemResult(WireModel):
    product_id: UUID
    http_status: Annotated[
        int,
        Field(
            ge=100,
            le=599,
            description=(
                "204 deleted; 404 unknown, another tenant's or already deleted; 409 a listing "
                "was sent to a marketplace (`INVALID_STATUS_TRANSITION`), is being sent "
                "(`SUBMISSION_IN_FLIGHT`) or is generating (`LISTING_BUSY`); 422 a size, which "
                "goes with its style."
            ),
        ),
    ]
    error: ErrorReport | None = None


class ProductBulkDeleteResult(WireModel):
    items: list[ProductDeleteItemResult]
