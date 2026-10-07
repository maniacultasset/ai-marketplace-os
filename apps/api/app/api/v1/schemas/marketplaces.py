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
SELLER_ID_PATTERN: Final = r"^[A-Za-z0-9]{1,64}$"
"""A marketplace seller id (D4): letters and digits only, so it can never inject a path segment
into a later marketplace URL. The real formats are confirmed with the marketplaces (T77)."""
PENDING_HANDLE_PATTERN: Final = r"^[A-Za-z0-9_-]{43}$"
"""The callback's opaque handle: `secrets.token_urlsafe(32)`, 43 URL-safe characters."""
CALLBACK_LOCATION_TEMPLATE: Final = "/marketplaces/connected?pending={pending}"
"""The FE route the callback redirects to; S15 makes the base URL a setting (ENV.md)."""


class ConnectRequest(StrictModel):
    marketplace: Marketplace


class ConnectConfirmRequest(StrictModel):
    """The handle the callback put in `?pending=`; only the user who started the flow, in the
    tenant that started it, can confirm it."""

    pending: Annotated[str, Field(pattern=PENDING_HANDLE_PATTERN)]


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
