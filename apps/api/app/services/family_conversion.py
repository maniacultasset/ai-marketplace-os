"""A saved standalone product becoming a family's style (`POST /products/{id}/size-run` and a
CSV import with `parent_style_id` naming it, the variants screen's "Save variants").

A style owns no barcode, stock reference or own size: those dictionary keys have
`owner_levels` `variant`/`standalone` only, and `facts_guard()` (0007) re-checks every stored
key when the row's level moves, so a standalone still holding one cannot be given a theme
(PRV03 `FACT_LEVEL_INVALID`). `blocker` names what the seller must move first. A GTIN
exemption is the one identity fact that moves on: `drop_identity` clears it from the row (its
columns and its `identifier` provenance) and says whether the new variants inherit it.

Its listings were written for one product: `listings` refuses a family whose listing the
marketplace has seen or one being generated, and `supersede` sends a validated, needing-review
or approved one back to `blocked/validation_errors` (the approval voided), as a recategorise
does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from mkos_schemas.codes import ErrorCode

from app.core.errors import ConflictError
from app.db.models.catalog import Product
from app.repositories.listings import Actor
from app.repositories.uow import TenantUnitOfWork
from app.services.product_recategorize import SEEN, SUPERSEDED

__all__ = ["IDENTITY_KEY", "blocker", "drop_identity", "listings", "supersede"]

IDENTITY_KEY: Final = "identifier"


def blocker(row: Product) -> str | None:
    """The request field a standalone holds that no style may own, or None."""
    attrs = row.attributes if isinstance(row.attributes, Mapping) else {}
    if row.identifier_value is not None:
        return "product_identifier"
    if row.stock_ref is not None:
        return "stock_ref"
    if "size" in attrs:
        return "attributes.size"
    return None


async def drop_identity(uow: TenantUnitOfWork, row: Product) -> bool:
    """Clear the standalone's exemption (or `not_applicable`) before it takes a theme; True
    when it was GTIN-exempt, so every new variant without a barcode is exempt too. The update
    expires `row`: the caller reads nothing from it afterwards."""
    exempt = row.identifier_status == "exempt"
    provenance = dict(row.provenance or {})
    if row.identifier_status is not None or IDENTITY_KEY in provenance:
        provenance.pop(IDENTITY_KEY, None)
        await uow.products.update_one(
            row.id, identifier_type=None, identifier_status=None, provenance=provenance
        )
    return exempt


async def listings(uow: TenantUnitOfWork, row: Product) -> list[Any]:
    """The product's listings, locked; 409 when one was seen by a marketplace or is busy."""
    found = await uow.listings.for_product_for_update(row.id)
    for listing in found:
        if (
            listing.status in SEEN
            or listing.first_published_at is not None
            or await uow.submissions.latest(listing.id) is not None
        ):
            raise ConflictError(ErrorCode.INVALID_STATUS_TRANSITION, "listing_published")
        if listing.status == "generating":
            raise ConflictError(ErrorCode.LISTING_BUSY, "listings")
    return found


async def supersede(uow: TenantUnitOfWork, found: Sequence[Any], actor: Actor) -> None:
    for listing in found:
        if listing.status in SUPERSEDED and listing.paused_at is None:
            await uow.listings.move(
                listing,
                to="blocked",
                event="revalidate",
                reason="validation_errors",
                actor=actor,
                clear_approval=listing.status == "approved",
            )
