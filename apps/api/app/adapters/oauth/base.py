"""The OAuth client seam (S15): one client per marketplace builds its consent URL, exchanges the
callback's code and refreshes a token pair over the marketplace's token endpoint.

These clients never unseal anything (they do not import `app.adapters.credentials`), so the API's
services can call them: the callback's exchange gets fresh tokens and seals them at once. The
refresh is called only by the worker, which unseals first.

Outcomes are three exceptions, none of which carries the response body or any token:
`OAuthRejectedError` (the marketplace said `invalid_grant`: the user must reconnect),
`OAuthTransientError` (a 5xx, a 429, a timeout or a connection error: retry later) and
`OAuthProtocolError` (anything else: our configuration or a reply we cannot trust, such as
`invalid_client`, a non-JSON or oversized body, a missing field, an out-of-range `expires_in` or a
seller id that fails `SELLER_ID_RE`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from app.adapters.oauth import http
from app.core.crypto import TokenPair

SELLER_ID_RE: Final = re.compile(r"^[A-Za-z0-9]{1,64}$")
"""D4: letters and digits only (the API's `SELLER_ID_PATTERN`). The real formats are confirmed
with the marketplaces (T77); until DW9's verification call a seller id is unverified."""
MAX_BODY_BYTES: Final = 64 * 1024
MAX_EXPIRES_IN: Final = 86_400
MAX_TOKEN_CHARS: Final = 4096
MAX_SCOPES: Final = 64


class OAuthError(Exception):
    """Base of the three outcomes; the message is fixed and carries no marketplace data."""

    message: str = "oauth error"

    def __init__(self) -> None:
        super().__init__(self.message)


class OAuthRejectedError(OAuthError):
    message = "the marketplace rejected the grant"


class OAuthTransientError(OAuthError):
    message = "the marketplace token endpoint is unavailable"


class OAuthProtocolError(OAuthError):
    message = "the marketplace token reply was not usable"


@dataclass(frozen=True, slots=True)
class CallbackParams:
    """What the callback received besides `state`: the code (Flipkart `code`, Amazon
    `spapi_oauth_code`) and Amazon's `selling_partner_id` (already pattern-checked)."""

    code: str = field(repr=False)
    selling_partner_id: str | None = None


@dataclass(frozen=True, slots=True)
class TokenGrant:
    tokens: TokenPair
    expires_at: datetime | None
    scopes: tuple[str, ...]
    seller_id: str | None
    region: str | None


class OAuthClient(Protocol):
    @property
    def marketplace(self) -> str: ...

    @property
    def uses_pkce(self) -> bool: ...

    def authorize_url(self, *, state: str, code_challenge: str | None) -> str: ...

    async def exchange(
        self, *, code_verifier: str | None, callback: CallbackParams
    ) -> TokenGrant: ...

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant: ...


def with_query(base: str, params: dict[str, str]) -> str:
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{urlencode(params)}"


async def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise OAuthProtocolError
    return bytes(body)


async def post_token(url: str, form: dict[str, str]) -> dict[str, Any]:
    """POST the form to the token endpoint; the JSON object on 200, else one of the outcomes.
    The body is read at most 64 KiB, redirects are never followed."""
    failure: type[OAuthError] | None = None
    status = 0
    raw = b""
    try:
        async with (
            http.oauth_http_client() as client,
            client.stream("POST", url, data=form, headers={"Accept": "application/json"}) as reply,
        ):
            status = reply.status_code
            raw = await _read_capped(reply)
    except OAuthProtocolError:
        failure = OAuthProtocolError
    except (httpx.TimeoutException, httpx.TransportError):
        failure = OAuthTransientError
    if failure is not None:
        raise failure from None
    if status >= 500 or status == 429:
        raise OAuthTransientError
    body = _json(raw)
    if 400 <= status < 500:
        if isinstance(body, dict) and body.get("error") == "invalid_grant":
            raise OAuthRejectedError
        raise OAuthProtocolError
    if status != 200 or not isinstance(body, dict):
        raise OAuthProtocolError
    return body


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _token(body: dict[str, Any], name: str) -> SecretStr:
    value = body.get(name)
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TOKEN_CHARS:
        raise OAuthProtocolError
    if not value.isprintable() or value.strip() != value:
        raise OAuthProtocolError
    return SecretStr(value)


def _expires_at(body: dict[str, Any], now: datetime) -> datetime:
    value = body.get("expires_in")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OAuthProtocolError
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise OAuthProtocolError
    seconds = int(value)
    if not 1 <= seconds <= MAX_EXPIRES_IN:
        raise OAuthProtocolError
    return now + timedelta(seconds=seconds)


def _scopes(body: dict[str, Any]) -> tuple[str, ...]:
    value = body.get("scope")
    if value is None:
        return ()
    if isinstance(value, str):
        parts = tuple(p for p in value.split() if p)
    elif isinstance(value, list) and all(isinstance(p, str) for p in value):
        parts = tuple(value)
    else:
        raise OAuthProtocolError
    if len(parts) > MAX_SCOPES or any(len(p) > 256 for p in parts):
        raise OAuthProtocolError
    return parts


def parse_grant(
    body: dict[str, Any],
    *,
    seller_id: str | None,
    keep_refresh: SecretStr | None = None,
    now: datetime | None = None,
) -> TokenGrant:
    """A `TokenGrant` from a token reply. A refresh reply may omit `refresh_token` (the old one
    is kept); an exchange reply must carry both tokens."""
    access = _token(body, "access_token")
    if keep_refresh is not None and body.get("refresh_token") is None:
        refresh = keep_refresh
    else:
        refresh = _token(body, "refresh_token")
    if seller_id is not None and not SELLER_ID_RE.fullmatch(seller_id):
        raise OAuthProtocolError
    return TokenGrant(
        tokens=TokenPair(access_token=access, refresh_token=refresh),
        expires_at=_expires_at(body, now or datetime.now(UTC)),
        scopes=_scopes(body),
        seller_id=seller_id,
        region=None,
    )
