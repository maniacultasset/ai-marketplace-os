"""The catalog reads (owner step SI12): what each marketplace needs for a category. Any member.
Platform rows only, so no tenant data is involved."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.catalog import RequirementLevelsOut, RequirementsOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services.requirements import get_requirements

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
CategoryQuery = Annotated[str, Query(pattern=r"^[a-z0-9_]{2,64}$", examples=["tshirts"])]


@route(router, OPS["catalog_requirements_get"])
async def catalog_requirements_get(
    category: CategoryQuery, principal: CurrentPrincipal
) -> RequirementsOut:
    """The dictionary keys Amazon and Flipkart need for a listing of `category`: each
    marketplace's required facts, the category template's required attributes and a reviewed
    extras table (Flipkart: HSN, package size and weight; Amazon: a barcode or the GTIN
    exemption, and the audience for Kurta sets and T-shirts). Shown as "Needed"; the extras are
    never sent. `levels` gives each marketplace template's requirement level per attribute
    (`required`, `conditional`, `recommended`, `optional`, `unsupported`); a missing
    `recommended` one is a non-blocking warning on review. An unknown category answers the facts
    and extras only, with empty levels."""
    found = await get_requirements(principal, category)
    return RequirementsOut(
        category=found.category,
        amazon=list(found.amazon),
        flipkart=list(found.flipkart),
        levels=RequirementLevelsOut.model_validate(
            {m.value: dict(v) for m, v in found.levels.items()}
        ),
    )
