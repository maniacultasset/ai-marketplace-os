"""Marketplace connect and account models (owner step S15).

`MarketplaceAccountOut` carries no token, secret or key field: credentials never leave the
adapter layer (invariant 6). The OAuth callback has no response model: it answers `303 See Other`
to a fixed FE route carrying only an opaque single-use `pending` handle (S3 CP4).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from mkos_schemas.base import NonBlankStr, StrictModel, WireModel
from mkos_schemas.enums import Marketplace
from pydantic import AwareDatetime, Field

from app.api.pagination import Page

OAUTH_PARAM_MAX: Final = 2048
CALLBACK_LOCATION_TEMPLATE: Final = "/marketplaces/connected?pending={pending}"
"""The FE route the callback redirects to; S15 makes the base URL a setting (ENV.md)."""


class ConnectRequest(StrictModel):
    marketplace: Marketplace


class ConnectOut(WireModel):
    authorize_url: Annotated[NonBlankStr, Field(max_length=OAUTH_PARAM_MAX)]


class MarketplaceAccountOut(WireModel):
    id: UUID
    marketplace: Marketplace
    seller_id: NonBlankStr
    region: str | None = None
    status: Literal["active", "needs_reconnect", "revoked"]
    needs_reconnect_since: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    created_at: AwareDatetime


class MarketplaceAccountPage(Page[MarketplaceAccountOut]):
    pass
