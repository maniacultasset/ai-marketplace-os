"""The Flipkart listings v3 HTTP client (spec L5300-5304, L11973; S22 T10; C6, C8). Believed, DW1.

One `httpx.AsyncClient` per adapter session: no redirects, no environment (proxies, netrc),
connect 5 s / read 10 s, and each reply read through a 1 MiB cap.

- `create(listings)`, `update(listings)`: one POST of a batch (`{"listings": [...]}`);
  `get(skus)`: one GET of the comma-joined seller SKUs. The adapter chunks (`CHUNK`).
- Before every try the outbound limiter admits the call (`mkt:flipkart:<account>:<op>`).
- 429: at most 3 retries (4 tries), delay `k` = `min(60, 2**k + uniform(0, 0.5))` with the
  injected `sleep` and `rng` (S20 C4); past the budget -> `RateLimited`.
- 401/403: one forced token refresh per session, then one retry; after the fresh token a 401
  is `AuthExpired`, a 403 `PermissionDenied` (S20 C3).
- Outcomes (S20 C2): a failure before the request was sent (connect error, pool timeout) is
  `MarketplaceUnavailable`; after a write was sent, a 5xx, a timeout, a redirect, an oversized or
  non-JSON body is `MarketplaceAmbiguous` (read the listing before any retry); any failure of a
  GET is `MarketplaceUnavailable`. Any other answer (2xx, 400, 404) goes back to the adapter.
- C8: a seller SKU holding `,` or `/`, or equal to `.` or `..`, is refused (`ValueError`) before
  the limiter or the token is touched, and every built request's raw path must equal the
  template with exactly the encoded SKUs.
- One log line `marketplace.call` per try: operation, method, path template, duration, status,
  the first error code and the try number. Never a URL, SKU, body, header or token. The token is
  a `SecretStr` until the header is built.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote, urlsplit

import httpx
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import Marketplace
from mkos_schemas.errors import ErrorReport
from pydantic import SecretStr

from app.adapters.base import (
    AdapterError,
    AuthExpired,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
    OutboundLimiter,
    PermissionDenied,
    RateLimited,
    TokenSource,
    marketplace_failure,
)
from app.adapters.flipkart import constants as c
from app.adapters.flipkart.mapping import unsafe_sku
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
MAX_BODY: Final = 1024 * 1024
MAX_RETRIES_429: Final = 3
BACKOFF_CAP_S: Final = 60.0
JITTER_S: Final = 0.5
_BEFORE_SEND: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_PATHS: Final = {"create": c.CREATE_PATH, "update": c.UPDATE_PATH, "get": c.GET_PATH}


class Rng(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True)
class FlipkartResponse:
    status: int
    json: Any
    request_id: str | None
    raw: bytes


def backoff_delay(k: int, rng: Rng) -> float:
    """The delay before retry `k` (0-based)."""
    return min(BACKOFF_CAP_S, 2.0**k + rng.uniform(0.0, JITTER_S))


def failure(code: ErrorCode, message: str) -> ErrorReport:
    return marketplace_failure(Marketplace.FLIPKART, code, message)


class _TooLarge(Exception):  # noqa: N818 - private signal
    pass


def _first_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    rows = body.get("errors")
    if not (isinstance(rows, list) and rows):
        results = body.get("results")
        if isinstance(results, list):
            rows = [e for r in results if isinstance(r, dict) for e in r.get("errors") or []]
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        code = rows[0].get("code")
        return str(code)[:64] if code is not None else None
    return None


def _check_skus(skus: Sequence[str]) -> None:
    if not skus:
        raise ValueError("no seller SKU")
    for sku in skus:
        if not isinstance(sku, str) or not sku or unsafe_sku(sku):
            raise ValueError("refused seller SKU")


class FlipkartClient:
    def __init__(
        self,
        *,
        api_base: str,
        tokens: TokenSource,
        limiter: OutboundLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Rng | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_body: int = MAX_BODY,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._base_path = urlsplit(self._base).path
        self._tokens = tokens
        self._limiter = limiter
        self._sleep = sleep
        self._rng: Rng = rng or random.SystemRandom()
        self._clock = clock
        self._max_body = max_body
        self._refreshed = False
        self._used: list[SecretStr] = []
        self._http = httpx.AsyncClient(
            transport=transport, timeout=TIMEOUT, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return "FlipkartClient()"

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def refreshed(self) -> bool:
        return self._refreshed

    def used_secrets(self) -> list[str]:
        """The token values this session sent, so the adapter can scrub them from any raw
        marketplace text it keeps (an error that echoes the token). Never logged."""
        return [s.get_secret_value() for s in self._used]

    # ---- the three calls ----

    async def create(self, listings: Sequence[Mapping[str, Any]]) -> FlipkartResponse:
        return await self._call("create", "POST", [str(e.get("sku")) for e in listings], listings)

    async def update(self, listings: Sequence[Mapping[str, Any]]) -> FlipkartResponse:
        return await self._call("update", "POST", [str(e.get("sku")) for e in listings], listings)

    async def get(self, skus: Sequence[str]) -> FlipkartResponse:
        return await self._call("get", "GET", list(skus), None)

    # ---- internals ----

    def _target(self, operation: str, skus: Sequence[str]) -> tuple[str, bytes]:
        _check_skus(skus)
        path = _PATHS[operation]
        if operation == "get":
            path = path.format(sku_ids=",".join(quote(s, safe="") for s in skus))
        raw = f"{self._base_path}{path}"
        return self._base + path, raw.encode("ascii")

    def _request(
        self, operation: str, method: str, skus: Sequence[str], content: bytes | None, token: str
    ) -> httpx.Request:
        url, expected = self._target(operation, skus)
        headers = {"authorization": f"Bearer {token}", "accept": "application/json"}
        if content is not None:
            headers["content-type"] = "application/json"
        request = self._http.build_request(method, url, content=content, headers=headers)
        if request.url.raw_path != expected:  # C8: no dot segment or re-encoding reaches the wire
            raise ValueError("request path differs from the Flipkart template")
        return request

    async def _read(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():  # decoded: the cap holds for gzip too
            size += len(chunk)
            if size > self._max_body:
                raise _TooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    def _log(
        self, operation: str, method: str, started: float, status: int | None, **extra: Any
    ) -> None:
        log.info(
            "marketplace.call",
            marketplace="flipkart",
            operation=operation,
            method=method,
            path=_PATHS[operation],
            duration_ms=round((self._clock() - started) * 1000, 1),
            status=status,
            **extra,
        )

    def _unknown(self, write: bool, message: str) -> AdapterError:
        if write:
            return MarketplaceAmbiguous(failure(ErrorCode.MARKETPLACE_UNCONFIRMED, message))
        return MarketplaceUnavailable(failure(ErrorCode.MARKETPLACE_UNAVAILABLE, message))

    async def _call(
        self,
        operation: str,
        method: str,
        skus: Sequence[str],
        listings: Sequence[Mapping[str, Any]] | None,
    ) -> FlipkartResponse:
        self._target(operation, skus)  # refuse before the limiter or the token is touched
        content = None
        if listings is not None:
            body = {"listings": list(listings)}
            content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        write = method != "GET"
        retries = 0
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire(operation)
            secret = await self._tokens.get()
            if all(secret is not seen for seen in self._used):
                self._used.append(secret)
            request = self._request(operation, method, skus, content, secret.get_secret_value())
            del secret
            started = self._clock()
            try:
                response = await self._http.send(request, stream=True)
            except _BEFORE_SEND as exc:
                self._log(
                    operation, method, started, None, attempt=attempt, error=type(exc).__name__
                )
                raise MarketplaceUnavailable(
                    failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "Flipkart could not be reached.")
                ) from None
            except httpx.HTTPError as exc:
                self._log(
                    operation, method, started, None, attempt=attempt, error=type(exc).__name__
                )
                raise self._unknown(write, "Flipkart did not answer the request in time.") from None
            try:
                raw = await self._read(response)
            except _TooLarge:
                self._log(operation, method, started, response.status_code, attempt=attempt)
                raise self._unknown(write, "Flipkart answered with an oversized reply.") from None
            except httpx.HTTPError as exc:
                self._log(
                    operation,
                    method,
                    started,
                    response.status_code,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                raise self._unknown(write, "Flipkart's reply was cut off.") from None
            finally:
                await response.aclose()
            status = response.status_code
            parsed: Any = None
            parsed_ok = True
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed_ok = False
            self._log(
                operation,
                method,
                started,
                status,
                attempt=attempt,
                error_code=_first_code(parsed) if parsed_ok else None,
            )
            if status == 429:
                if retries >= MAX_RETRIES_429:
                    raise RateLimited(
                        failure(ErrorCode.RATE_LIMITED, "Flipkart throttled the request.")
                    )
                await self._sleep(backoff_delay(retries, self._rng))
                retries += 1
                continue
            if status in (401, 403):
                if not self._refreshed:
                    self._refreshed = True
                    await self._tokens.refresh()
                    continue
                if status == 401:
                    raise AuthExpired(
                        failure(ErrorCode.AUTH_EXPIRED, "Flipkart refused the account's token.")
                    )
                raise PermissionDenied(
                    failure(
                        ErrorCode.ACCOUNT_PERMISSION_DENIED,
                        "The Flipkart account did not grant this application.",
                    )
                )
            if 300 <= status < 400:
                raise self._unknown(write, "Flipkart answered with a redirect.")
            if status >= 500:
                raise self._unknown(write, "Flipkart answered with a server error.")
            if not parsed_ok:
                raise self._unknown(write, "Flipkart answered with a body that is not JSON.")
            request_id = parsed.get("request_id") if isinstance(parsed, dict) else None
            return FlipkartResponse(
                status=status,
                json=parsed,
                request_id=str(request_id)[:128] if request_id else None,
                raw=raw,
            )
