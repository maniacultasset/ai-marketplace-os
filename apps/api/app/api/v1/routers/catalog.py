"""The catalog reads (owner step SI12): what each marketplace needs for a category. Any member.
Platform rows only, so no tenant data is involved.

Client feedback 11 Nov (item 5): the categories a seller can pick, with what each marketplace is
sent for them (any member, platform rows only), and the category suggestion before a product is
saved (ask: `product:import`; read: any member of the tenant, another tenant's is 404). Routes
import no `sqlalchemy` or `app.db`; domain errors render through the shared handlers."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from app.api.errors import ApiError
from app.api.v1.deps import IdempotencyKey, get_current_principal
from app.api.v1.schemas.catalog import (
    CategoryCatalogOut,
    CategoryDraftIn,
    CategoryDraftSuggestionOut,
    CategoryOut,
    MarketplaceCategoryOut,
    PhotoViewRuleOut,
    RequirementsOut,
)
from app.api.v1.surface import OPS, route
from app.core.rbac import Principal
from app.services import category_draft
from app.services.budget import BudgetExceededError, CreditPausedError
from app.services.catalog_categories import list_categories
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


@route(router, OPS["catalog_categories_list"])
async def catalog_categories_list(principal: CurrentPrincipal) -> CategoryCatalogOut:
    """Every category with a current template, sorted by its plain name: whether it can be
    picked for a new product (`enabled`; lamps only while `CATEGORY_LAMPS_ENABLED` is on), and
    per marketplace the product type the listing sends, its plain name and the external
    category id (null until mapped). `suggest_enabled` says whether the category suggestion is
    switched on. The same answer for every tenant."""
    found = await list_categories(principal)
    return CategoryCatalogOut(
        items=[
            CategoryOut(
                category=item.category,
                label=item.label,
                enabled=item.enabled,
                marketplaces=[
                    MarketplaceCategoryOut.model_validate(m, from_attributes=True)
                    for m in item.marketplaces
                ],
            )
            for item in found.items
        ],
        suggest_enabled=found.suggest_enabled,
    )


@route(router, OPS["catalog_category_suggest"])
async def catalog_category_suggest(
    body: CategoryDraftIn,
    idempotency_key: IdempotencyKey,
    principal: CurrentPrincipal,
    request: Request,
    response: Response,
) -> CategoryDraftSuggestionOut:
    """Suggest up to 3 categories from what the seller typed so far (the name, and the
    description, brand and attribute values when given), before the product is saved. 202 names
    the suggestion to read with `GET /catalog/category-suggestions/{suggestion_id}`. Nothing is
    set or saved: the seller picks the category. The same draft asked again answers the earlier
    suggestion with nothing spent: 202 while it runs, 200 with its candidates within a day.
    While the suggestion is switched off (or no category can be offered) it is 200
    `unavailable` with nothing spent. While AI is paused for
    every brand it is 503 with the pause's code (SI10); past the daily cap 429."""
    draft = category_draft.Draft(
        title=body.title,
        description=body.description,
        brand=body.brand,
        attributes=dict(body.attributes),
    )
    try:
        status, answer = await category_draft.request_draft_suggestion(
            principal, idempotency_key, request.url.path, draft
        )
    except BudgetExceededError as exc:
        raise ApiError(
            429,
            ErrorCode.BUDGET_EXCEEDED,
            exc.message,
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from None
    except CreditPausedError as exc:
        raise ApiError(503, exc.code, exc.message) from None
    response.status_code = status
    return CategoryDraftSuggestionOut.model_validate(answer)


@route(router, OPS["catalog_category_suggestion_get"])
async def catalog_category_suggestion_get(
    suggestion_id: Annotated[UUID, PathParam()], principal: CurrentPrincipal
) -> CategoryDraftSuggestionOut:
    """One category suggestion: `queued` while it runs, then `suggested` with up to 3
    candidates (highest confidence first; `unsure` below 0.80), `unavailable` or `failed`.
    Another tenant's suggestion is 404, as a missing one."""
    answer = await category_draft.read_draft_suggestion(principal, suggestion_id)
    return CategoryDraftSuggestionOut.model_validate(answer)
