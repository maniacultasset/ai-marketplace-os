"""The seller check of an Amazon connect (V1 hardening #11).

The Amazon callback names the seller in `selling_partner_id`, a URL parameter the browser can
change, and a live seller id is unique across all tenants. So before a connect is stored, one
read-only SP-API call proves the fresh token may act for that seller: a Listings Items GET of a
fixed probe SKU under the claimed seller id (`SpApiClient.get`, the same route, limits and log
line as every other GET).

- 200 or 404 (the SKU is not there): Amazon accepted the seller id for this token, `verified`.
- 401 or 403 (no forced refresh: the token is the one just granted), 400, or any other 4xx:
  `refused`. The connect is refused, so an unverified seller id never reaches a pending row and
  never takes the global live slot.
- No answer, 5xx or 429: the adapter error is raised as is (the caller answers 503; the brand
  retries the connect).

The token stays a `SecretStr`; nothing here logs it, the seller id or the reply.
"""

from __future__ import annotations

import time
from typing import Final, Literal

import httpx
from mkos_schemas.codes import ErrorCode
from pydantic import SecretStr

from app.adapters.amazon.client import SpApiClient, failure
from app.adapters.base import PermissionDenied

PROBE_SKU: Final = "kuinn-seller-check"
VERIFIED_STATUSES: Final = frozenset({200, 404})
SellerCheck = Literal["verified", "refused"]


class _GrantTokens:
    """The just-granted access token. A 401/403 is the answer, never a reason to refresh."""

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    async def get(self) -> SecretStr:
        return self._token

    async def refresh(self) -> SecretStr:
        raise PermissionDenied(
            failure(ErrorCode.ACCOUNT_PERMISSION_DENIED, "Amazon refused the seller account.")
        )


class _OneCall:
    """No shared bucket: the check makes a single call per connect (the API limits connects)."""

    async def acquire(self, operation: str) -> float:
        return time.time()


async def verify_seller(
    *,
    endpoint: str,
    marketplace_id: str,
    seller_id: str,
    access_token: SecretStr,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SellerCheck:
    client = SpApiClient(
        endpoint=endpoint,
        marketplace_id=marketplace_id,
        seller_id=seller_id,
        tokens=_GrantTokens(access_token),
        limiter=_OneCall(),
        transport=transport,
    )
    try:
        reply = await client.get(PROBE_SKU)
    except PermissionDenied:
        return "refused"
    finally:
        await client.aclose()
    return "verified" if reply.status in VERIFIED_STATUSES else "refused"
