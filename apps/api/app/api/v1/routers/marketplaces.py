"""Marketplace stubs (owner step S15).

The callback is public: the tenant comes only from the server-side `oauth_states` row, consumed
once. It answers `303 See Other` to a fixed app route with only an opaque single-use `pending`
handle; S15's authenticated confirm step binds the account to the user who started the flow.
"""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi import Path as PathParam
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace

from app.api.errors import ApiError, NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.deps import get_current_principal
from app.api.v1.schemas.marketplaces import (
    CALLBACK_LOCATION_TEMPLATE,
    OAUTH_PARAM_MAX,
    SELLER_ID_PATTERN,
    ConnectConfirmRequest,
    ConnectOut,
    ConnectRequest,
    MarketplaceAccountOut,
)
from app.api.v1.surface import OPS, route, stub
from app.core.config import get_settings
from app.core.errors import ConflictError
from app.core.rbac import Principal
from app.services import marketplaces as service

router = APIRouter()
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
NO_STORE = "no-store"
OAuthParam = Annotated[str, Query(min_length=1, max_length=OAUTH_PARAM_MAX)]
OptionalOAuthParam = Annotated[str | None, Query(min_length=1, max_length=OAUTH_PARAM_MAX)]
SellerParam = Annotated[str | None, Query(pattern=SELLER_ID_PATTERN)]


def account_out(view: service.AccountView) -> MarketplaceAccountOut:
    """A DB-sourced row is built, not validated (the DB CHECKs are the read-side truth)."""
    return MarketplaceAccountOut.model_construct(
        id=view.id,
        marketplace=Marketplace(view.marketplace),
        seller_id=view.seller_id,
        region=view.region,
        status=view.status,
        needs_reconnect_since=view.needs_reconnect_since,
        expires_at=view.expires_at,
        created_at=view.created_at,
    )


def _one_code(code: str | None, spapi_oauth_code: str | None) -> str:
    """Flipkart returns `code`, Amazon `spapi_oauth_code`: exactly one, else 422."""
    if (code is None) == (spapi_oauth_code is None):
        raise ApiError(
            422,
            ErrorCode.VALIDATION_FAILED,
            details=[
                {"loc": ["query", "code"], "msg": "Send exactly one of code or spapi_oauth_code."}
            ],
        )
    return code if code is not None else spapi_oauth_code  # type: ignore[return-value]


@route(router, OPS["marketplaces_connect"])
async def marketplaces_connect(
    body: ConnectRequest, principal: CurrentPrincipal, response: Response
) -> ConnectOut:
    """Start OAuth for a marketplace: returns the consent URL (owner or admin). A marketplace
    that is not configured here answers 501 NOT_IMPLEMENTED and writes nothing."""
    try:
        url = await service.start_connect(principal, body.marketplace.value)
    except service.MarketplaceNotConfiguredError:
        raise NotImplementedYet("marketplaces_connect") from None
    response.headers["Cache-Control"] = NO_STORE
    return ConnectOut(authorize_url=url)


@route(router, OPS["marketplaces_callback"])
async def marketplaces_callback(
    marketplace: Annotated[Marketplace, PathParam()],
    state: OAuthParam,
    code: OptionalOAuthParam = None,
    spapi_oauth_code: OptionalOAuthParam = None,
    selling_partner_id: SellerParam = None,
) -> Response:
    """OAuth return (no JWT). Flipkart sends `code`; Amazon sends `spapi_oauth_code` and
    `selling_partner_id`. A forged, expired or replayed `state`, or a path marketplace that is not
    the state's, is 409 OAUTH_STATE_INVALID. The answer is a 303 to the dashboard's fixed route
    carrying only an opaque single-use `pending` handle."""
    the_code = _one_code(code, spapi_oauth_code)
    try:
        handle = await service.complete_callback(
            marketplace.value,
            state=state,
            code=the_code,
            selling_partner_id=selling_partner_id,
        )
    except service.MarketplaceNotConfiguredError:
        raise NotImplementedYet("marketplaces_callback") from None
    except service.MarketplaceUnavailableError:
        raise ApiError(503, ErrorCode.MARKETPLACE_UNAVAILABLE) from None
    except service.MarketplaceUnknownError:
        raise ApiError(502, ErrorCode.MARKETPLACE_UNKNOWN_ERROR) from None
    location = get_settings().dashboard_base_url + CALLBACK_LOCATION_TEMPLATE.format(pending=handle)
    return Response(
        status_code=303,
        headers={"Location": location, "Cache-Control": NO_STORE, "Referrer-Policy": "no-referrer"},
    )


@route(router, OPS["marketplaces_accounts"])
@stub
async def marketplaces_accounts(page: Annotated[PageParams, Depends()]) -> NoReturn:
    """Connected marketplace accounts and their connection health. No credential is returned."""
    raise NotImplementedYet("marketplaces_accounts")


@route(router, OPS["marketplaces_connect_confirm"])
async def marketplaces_connect_confirm(
    body: ConnectConfirmRequest, principal: CurrentPrincipal
) -> MarketplaceAccountOut:
    """Confirm the pending connection named by the callback's `pending` handle (owner or admin).
    Only the user who started the flow, in its tenant, can confirm it; an unknown, used, expired
    or foreign handle is 409 OAUTH_STATE_INVALID. A seller account already live (in this tenant
    under another seller, or in any other tenant) is 409 MARKETPLACE_ACCOUNT_CONFLICT: revoke it
    first."""
    try:
        view = await service.confirm(principal, body.pending)
    except ConflictError as exc:
        if exc.code is not ErrorCode.MARKETPLACE_ACCOUNT_CONFLICT:
            raise
        raise ApiError(409, exc.code, service.SELLER_CONFLICT_MESSAGE) from None
    return account_out(view)


@route(router, OPS["marketplaces_accounts_revoke"])
@stub
async def marketplaces_accounts_revoke(account_id: Annotated[UUID, PathParam()]) -> NoReturn:
    """Revoke a marketplace connection (owner or admin). This deletes the stored tokens only: the
    app stays authorized at the marketplace until the brand removes it in Seller Central (Amazon)
    or the Flipkart seller dashboard. Revoking an already revoked account returns it unchanged."""
    raise NotImplementedYet("marketplaces_accounts_revoke")
