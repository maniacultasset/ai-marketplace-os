"""The catalog reads (owner step SI12): what each marketplace needs for a category. Any member.
Platform rows only, so no tenant data is involved."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from mkos_schemas.enums import Marketplace

from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.catalog import PhotoViewRuleOut, RequirementsOut
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services.photo_views import VIEW_LABELS, VIEW_SLOTS, MergedView
from app.services.requirements import get_requirements

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
CategoryQuery = Annotated[str, Query(pattern=r"^[a-z0-9_]{2,64}$", examples=["tshirts"])]


def photo_view_fields(rule: MergedView) -> dict[str, Any]:
    """A merged checklist row as `PhotoViewRuleOut` fields (the product read adds its status)."""
    slot, role = VIEW_SLOTS[rule.view]
    return {
        "view": rule.view,
        "label": VIEW_LABELS[rule.view],
        "slot": slot,
        "role": role,
        "requirement": rule.requirement,
        "why": rule.why,
        "amazon": rule.by_marketplace.get(Marketplace.AMAZON),
        "flipkart": rule.by_marketplace.get(Marketplace.FLIPKART),
    }


@route(router, OPS["catalog_requirements_get"])
async def catalog_requirements_get(
    category: CategoryQuery, principal: CurrentPrincipal
) -> RequirementsOut:
    """The dictionary keys Amazon and Flipkart need for a listing of `category`: each
    marketplace's required facts, the category template's required attributes and a reviewed
    extras table (Flipkart: HSN, package size and weight; Amazon: a barcode or the GTIN
    exemption, and the audience for Kurta sets and T-shirts). Shown as "Needed"; the extras are
    never sent. An unknown category answers the facts and extras only. `photo_views` is the
    category's photo checklist (each view required, recommended or optional, and why)."""
    found = await get_requirements(principal, category)
    return RequirementsOut(
        category=found.category,
        amazon=list(found.amazon),
        flipkart=list(found.flipkart),
        photo_views=[
            PhotoViewRuleOut.model_construct(**photo_view_fields(r)) for r in found.photo_views
        ],
    )
