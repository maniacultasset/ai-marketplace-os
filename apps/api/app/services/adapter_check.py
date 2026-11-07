"""The marketplace check before approval (SI9 B4; CP-D2, CP-D5).

The same pure previewer the preview route uses (`registry.previewer`, the stored Product Type
Definition included when one is current) runs on a version's content and gives its items. It
sends nothing (invariant 1) and the Rules Engine stays the arbiter of the Rules verdict
(invariant 2): these items are synced as issues under source `schema` (full scope, D6), never
under `<marketplace>_adapter`, whose rows belong to real marketplace answers.

- `check_source(source, schema)` is pure: None for a marketplace with no previewer; otherwise a
  report whose items are re-stamped `source = schema`, capped at `MAX_ERROR_ITEMS`, `failed`
  when any item is an ERROR. A bug in the check fails closed (CP-D5): one `INTERNAL_ERROR` item
  on `listing`, logged as `adapter_check.failed` with ids only.
- `adapter_check(uow, listing_id, version=...)` reads the source and the current definition,
  then checks. None when there is no listing or no version. A database error is raised, never
  turned into a report (the transaction is gone); any other failure of the definition read or
  the check fails closed as above.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource, Severity
from mkos_schemas.errors import MAX_ERROR_ITEMS, ErrorItem, ErrorReport
from sqlalchemy.exc import DBAPIError

from app.adapters.base import AdapterListing, ProductTypeSchema
from app.adapters.registry import previewer
from app.core.logging import get_logger
from app.repositories.listings import PreviewSource, SourceVersion
from app.services.adapter_input import build_adapter_listing, canonical_listing, colour_image_ids
from app.services.product_type_schema import current_schema

log = get_logger(__name__)

CHECK_FAILED_MESSAGE: Final = "The marketplace check could not run."


def adapter_listing_for(
    source: PreviewSource, schema: ProductTypeSchema | None, *, idempotency_key: str
) -> AdapterListing:
    """The adapter input of a preview or a check: images as opaque `image:<id>` references."""
    body = canonical_listing(source)
    ids = {image.image_id for image in body.images} | colour_image_ids(source)
    return build_adapter_listing(
        source,
        image_refs={image_id: f"image:{image_id}" for image_id in ids},
        idempotency_key=idempotency_key,
        product_type_schema=schema,
    )


def _failed(listing_id: uuid.UUID) -> ErrorReport:
    item = ErrorItem(
        field="listing",
        error_code=ErrorCode.INTERNAL_ERROR,
        message=CHECK_FAILED_MESSAGE,
        source=ErrorSource.SCHEMA,
        retryable=False,
    )
    return ErrorReport(
        status="failed",
        error_code=ErrorCode.INTERNAL_ERROR,
        errors=[item],
        listing_id=listing_id,
    )


def check_source(source: PreviewSource, schema: ProductTypeSchema | None) -> ErrorReport | None:
    try:
        preview = previewer(source.marketplace)
    except NotImplementedError:
        return None
    try:
        result = preview(
            adapter_listing_for(source, schema, idempotency_key=f"check:{source.listing_id}")
        )
    except Exception as exc:
        log.warning(
            "adapter_check.failed",
            listing_id=str(source.listing_id),
            error_class=type(exc).__name__,
        )
        return _failed(source.listing_id)
    items = [
        item.model_copy(update={"source": ErrorSource.SCHEMA})
        for item in result.report.errors[:MAX_ERROR_ITEMS]
    ]
    blocking = [item for item in items if item.severity is Severity.ERROR]
    return ErrorReport(
        status="failed" if blocking else "passed",
        error_code=blocking[0].error_code if blocking else None,
        errors=items,
        listing_id=source.listing_id,
    )


async def adapter_check(
    uow: Any, listing_id: uuid.UUID, *, version: SourceVersion | None = None
) -> ErrorReport | None:
    source = await uow.listings.preview_source(listing_id, version=version)
    if source is None or source.version is None:
        return None
    try:
        schema = None
        if source.template is not None:
            schema = await current_schema(
                uow, marketplace=source.marketplace, product_type=source.template.product_type
            )
    except DBAPIError:
        raise
    except Exception as exc:
        log.warning(
            "adapter_check.failed", listing_id=str(listing_id), error_class=type(exc).__name__
        )
        return _failed(listing_id)
    return check_source(source, schema)
