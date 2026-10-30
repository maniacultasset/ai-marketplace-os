"""Google's two HTTP calls for sign-in (ADR-0020): the authorization-code exchange and the JWKS
download. Both use the OAuth HTTP client (short timeouts, no redirects, no environment, no event
hooks), so nothing logs the token request body (the code, the PKCE verifier, the client secret)
or the reply (the ID token). Tests swap `http.make_transport` for the fake Google over ASGI.

Outcomes reuse the OAuth seam's three exceptions: `OAuthRejectedError` (`invalid_grant`: a used,
expired or forged code), `OAuthTransientError` (a 5xx, a 429, a timeout or a connection error)
and `OAuthProtocolError` (anything else). None carries a body or a token.
"""

from __future__ import annotations

from typing import Final

import httpx
from pydantic import SecretStr

from app.adapters.oauth import http
from app.adapters.oauth.base import (
    MAX_BODY_BYTES,
    OAuthError,
    OAuthProtocolError,
    OAuthTransientError,
    post_token,
)

MAX_ID_TOKEN_CHARS: Final = 8192


async def exchange_code(
    *,
    token_url: str,
    code: str,
    verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> SecretStr:
    """The ID token of an authorization code (PKCE-bound). Google's access token is never kept:
    only the identity is used."""
    body = await post_token(
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    value = body.get("id_token")
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ID_TOKEN_CHARS:
        raise OAuthProtocolError
    if not value.isascii() or value.strip() != value:
        raise OAuthProtocolError
    return SecretStr(value)


async def fetch_jwks(url: str) -> bytes:
    """The raw JWKS document (at most 64 KiB)."""
    failure: type[OAuthError] | None = None
    status = 0
    body = bytearray()
    try:
        async with (
            http.oauth_http_client() as client,
            client.stream("GET", url, headers={"Accept": "application/json"}) as reply,
        ):
            status = reply.status_code
            async for chunk in reply.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    raise OAuthProtocolError
    except OAuthProtocolError:
        failure = OAuthProtocolError
    except httpx.DecodingError:
        failure = OAuthProtocolError
    except httpx.HTTPError:
        failure = OAuthTransientError
    if failure is not None:
        raise failure from None
    if status >= 500 or status == 429:
        raise OAuthTransientError
    if status != 200:
        raise OAuthProtocolError
    return bytes(body)


__all__ = ["exchange_code", "fetch_jwks"]
