"""Marketplace stubs (owner step S15).

The callback is public: the tenant comes only from the server-side `oauth_states` row, consumed
once. It answers `303 See Other` to a fixed app route with only an opaque single-use `pending`
handle; S15's authenticated confirm step binds the account to the user who started the flow.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, Query
from fastapi import Path as PathParam
from mkos_schemas.enums import Marketplace

from app.api.errors import NotImplementedYet
from app.api.pagination import PageParams
from app.api.v1.schemas.marketplaces import OAUTH_PARAM_MAX, ConnectRequest
from app.api.v1.surface import OPS, route, stub

router = APIRouter()
OAuthParam = Annotated[str, Query(min_length=1, max_length=OAUTH_PARAM_MAX)]


@route(router, OPS["marketplaces_connect"])
@stub
async def marketplaces_connect(body: ConnectRequest) -> NoReturn:
    """Start OAuth for a marketplace: returns the consent URL (owner or admin)."""
    raise NotImplementedYet("marketplaces_connect")


@route(router, OPS["marketplaces_callback"])
@stub
async def marketplaces_callback(
    marketplace: Annotated[Marketplace, PathParam()], code: OAuthParam, state: OAuthParam
) -> NoReturn:
    """OAuth return (no JWT). A forged, expired or replayed `state`, or a path marketplace that
    is not the state's, is 409 OAUTH_STATE_INVALID."""
    raise NotImplementedYet("marketplaces_callback")


@route(router, OPS["marketplaces_accounts"])
@stub
async def marketplaces_accounts(page: Annotated[PageParams, Depends()]) -> NoReturn:
    """Connected marketplace accounts and their connection health. No credential is returned."""
    raise NotImplementedYet("marketplaces_accounts")
