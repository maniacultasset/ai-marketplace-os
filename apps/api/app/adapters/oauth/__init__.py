"""Marketplace OAuth clients (S15). `get_oauth_client()` is None for a marketplace that is not
configured (client id, secret or authorize URL unset; for Amazon also `AMAZON_APP_ID`), and the
connect and callback routes then answer 501 before any write."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.oauth.amazon import AmazonOAuthClient
from app.adapters.oauth.base import (
    SELLER_ID_RE,
    CallbackParams,
    OAuthClient,
    OAuthError,
    OAuthProtocolError,
    OAuthRejectedError,
    OAuthTransientError,
    TokenGrant,
)
from app.adapters.oauth.flipkart import FlipkartOAuthClient

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "SELLER_ID_RE",
    "CallbackParams",
    "OAuthClient",
    "OAuthError",
    "OAuthProtocolError",
    "OAuthRejectedError",
    "OAuthTransientError",
    "TokenGrant",
    "get_oauth_client",
]


def _set(*values: str | None) -> bool:
    return all(value is not None and value.strip() for value in values)


def get_oauth_client(marketplace: str, settings: Settings | None = None) -> OAuthClient | None:
    if settings is None:
        from app.core.config import get_settings

        settings = get_settings()
    s = settings
    if marketplace == "amazon":
        if not _set(s.amazon_app_id, s.amazon_client_id, s.amazon_client_secret):
            return None
        if not _set(s.amazon_authorize_url):
            return None
        return AmazonOAuthClient(
            application_id=str(s.amazon_app_id),
            client_id=str(s.amazon_client_id),
            client_secret=str(s.amazon_client_secret),
            authorize_base=str(s.amazon_authorize_url),
            token_url=s.amazon_lwa_token_url,
            redirect_uri=s.amazon_oauth_redirect_uri,
        )
    if marketplace == "flipkart":
        if not _set(s.flipkart_client_id, s.flipkart_client_secret, s.flipkart_authorize_url):
            return None
        return FlipkartOAuthClient(
            client_id=str(s.flipkart_client_id),
            client_secret=str(s.flipkart_client_secret),
            authorize_base=str(s.flipkart_authorize_url),
            token_url=s.flipkart_token_url,
            redirect_uri=s.flipkart_oauth_redirect_uri,
        )
    return None
