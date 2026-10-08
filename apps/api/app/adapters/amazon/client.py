"""The SP-API Listings Items HTTP client (spec L10561, L11973, L12017, L12719; S20 T10).

One `httpx.AsyncClient` per adapter session: no redirects, no environment (proxies, netrc),
connect 5 s / read 10 s, and each reply read through a 1 MiB cap.

- Before every try the outbound limiter admits the call (`mkt:` buckets, T13).
- 429: at most 3 retries (4 tries), delay `k` = `min(60, 2**k + uniform(0, 0.5))` with the
  injected `sleep` and `rng` (C4); past the budget -> `RateLimited`.
- 401/403: one forced token refresh per session, then one retry; after the fresh token a 401
  is `AuthExpired`, a 403 `PermissionDenied` (C3). A refused refresh raises from the token
  source.
- C2 outcomes: a failure before the request was sent (connect error, pool timeout) is
  `MarketplaceUnavailable`; after a write was sent, a 5xx, a timeout, a redirect, an oversized or
  non-JSON body is `MarketplaceAmbiguous` (read the listing before any retry); any failure of a
  GET is `MarketplaceUnavailable`. Any other answer (2xx, 400, 404) goes back to the adapter.
- C16: a SKU of `.` or `..` is refused, and every built request's raw path must equal the
  template with exactly the encoded seller and SKU segments, before anything is sent.
- One log line `marketplace.call` per try: operation, method, path template, duration, status,
  the rate-limit header, the first issue code and the try number. Never a URL, SKU, body,
  header or token. The token is a `SecretStr` until the header is built.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote, urlencode

import httpx
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport

from app.adapters.amazon import constants as c
from app.adapters.base import (
    AdapterError,
    AuthExpired,
    MarketplaceAmbiguous,
    MarketplaceUnavailable,
    OutboundLimiter,
    PermissionDenied,
    RateLimited,
    TokenSource,
)
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
MAX_BODY: Final = 1024 * 1024
MAX_RETRIES_429: Final = 3
BACKOFF_CAP_S: Final = 60.0
JITTER_S: Final = 0.5
PATH_TEMPLATE: Final = f"{c.ITEMS_PATH}/{{sellerId}}/{{sku}}"
UNSAFE_SKUS: Final = frozenset({".", ".."})
GET_INCLUDED: Final = "summaries,attributes,issues"
_RETRYABLE: Final = frozenset({RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT})
_BEFORE_SEND: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class Rng(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True)
class SpApiResponse:
    status: int
    json: Any
    headers: Mapping[str, str]
    raw: bytes


def backoff_delay(k: int, rng: Rng) -> float:
    """The delay before retry `k` (0-based)."""
    return min(BACKOFF_CAP_S, 2.0**k + rng.uniform(0.0, JITTER_S))


def failure(code: ErrorCode, message: str) -> ErrorReport:
    meta = ERROR_CODE_META[code]
    retryable = meta.retry_class in _RETRYABLE
    item = ErrorItem(
        field="listing",
        error_code=code,
        message=message,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=retryable,
        marketplace=Marketplace.AMAZON,
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )
    return ErrorReport(status="failed", error_code=code, retryable=retryable, errors=[item])


class _TooLarge(Exception):  # noqa: N818 - private signal
    pass


def _first_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("issues", "errors"):
        rows = body.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            code = rows[0].get("code")
            return str(code)[:64] if code is not None else None
    return None


class SpApiClient:
    def __init__(
        self,
        *,
        endpoint: str,
        marketplace_id: str,
        seller_id: str,
        tokens: TokenSource,
        limiter: OutboundLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Rng | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_body: int = MAX_BODY,
    ) -> None:
        if not seller_id or seller_id in UNSAFE_SKUS:
            raise ValueError("seller_id is required")
        self._endpoint = endpoint.rstrip("/")
        self._marketplace_id = marketplace_id
        self._seller = seller_id
        self._tokens = tokens
        self._limiter = limiter
        self._sleep = sleep
        self._rng: Rng = rng or random.SystemRandom()
        self._clock = clock
        self._max_body = max_body
        self._refreshed = False
        self._http = httpx.AsyncClient(
            transport=transport, timeout=TIMEOUT, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return "SpApiClient()"

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def refreshed(self) -> bool:
        return self._refreshed

    # ---- the three calls ----

    async def put(self, sku: str, body: Mapping[str, Any]) -> SpApiResponse:
        return await self._call("put", "PUT", sku, body)

    async def patch(self, sku: str, body: Mapping[str, Any]) -> SpApiResponse:
        return await self._call("patch", "PATCH", sku, body)

    async def get(self, sku: str) -> SpApiResponse:
        return await self._call("get", "GET", sku, None)

    # ---- internals ----

    def _target(self, method: str, sku: str) -> tuple[str, bytes]:
        if not isinstance(sku, str) or not sku or sku.strip() in UNSAFE_SKUS:
            raise ValueError("refused seller SKU")
        path = f"{c.ITEMS_PATH}/{quote(self._seller, safe='')}/{quote(sku, safe='')}"
        query: dict[str, str] = {"marketplaceIds": self._marketplace_id}
        if method == "GET":
            query["includedData"] = GET_INCLUDED
        raw = f"{path}?{urlencode(query, safe='')}"
        return self._endpoint + raw, raw.encode("ascii")

    def _request(self, method: str, sku: str, content: bytes | None, token: str) -> httpx.Request:
        url, expected = self._target(method, sku)
        headers = {"x-amz-access-token": token, "accept": "application/json"}
        if content is not None:
            headers["content-type"] = "application/json"
        request = self._http.build_request(method, url, content=content, headers=headers)
        if request.url.raw_path != expected:  # C16: no dot segment or re-encoding reaches the wire
            raise ValueError("request path differs from the SP-API template")
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
            marketplace="amazon",
            operation=operation,
            method=method,
            path=PATH_TEMPLATE,
            duration_ms=round((self._clock() - started) * 1000, 1),
            status=status,
            **extra,
        )

    def _unknown(self, write: bool, message: str) -> AdapterError:
        if write:
            return MarketplaceAmbiguous(failure(ErrorCode.MARKETPLACE_UNCONFIRMED, message))
        return MarketplaceUnavailable(failure(ErrorCode.MARKETPLACE_UNAVAILABLE, message))

    async def _call(
        self, operation: str, method: str, sku: str, body: Mapping[str, Any] | None
    ) -> SpApiResponse:
        self._target(method, sku)  # refuse before the limiter or the token is touched
        content = None
        if body is not None:
            content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        write = method != "GET"
        retries = 0
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.acquire(operation)
            secret = await self._tokens.get()
            request = self._request(method, sku, content, secret.get_secret_value())
            del secret
            started = self._clock()
            try:
                response = await self._http.send(request, stream=True)
            except _BEFORE_SEND as exc:
                self._log(
                    operation, method, started, None, attempt=attempt, error=type(exc).__name__
                )
                raise MarketplaceUnavailable(
                    failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "Amazon could not be reached.")
                ) from None
            except httpx.HTTPError as exc:
                self._log(
                    operation, method, started, None, attempt=attempt, error=type(exc).__name__
                )
                raise self._unknown(write, "Amazon did not answer the request in time.") from None
            try:
                raw = await self._read(response)
            except _TooLarge:
                self._log(operation, method, started, response.status_code, attempt=attempt)
                raise self._unknown(write, "Amazon answered with an oversized reply.") from None
            except httpx.HTTPError as exc:
                self._log(
                    operation,
                    method,
                    started,
                    response.status_code,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                raise self._unknown(write, "Amazon's reply was cut off.") from None
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
                rate_limit=response.headers.get("x-amzn-RateLimit-Limit"),
                issue_code=_first_code(parsed) if parsed_ok else None,
            )
            if status == 429:
                if retries >= MAX_RETRIES_429:
                    raise RateLimited(
                        failure(ErrorCode.RATE_LIMITED, "Amazon throttled the request.")
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
                        failure(ErrorCode.AUTH_EXPIRED, "Amazon refused the account's token.")
                    )
                raise PermissionDenied(
                    failure(
                        ErrorCode.ACCOUNT_PERMISSION_DENIED,
                        "The Amazon account did not grant the Listings role to this app.",
                    )
                )
            if 300 <= status < 400:
                raise self._unknown(write, "Amazon answered with a redirect.")
            if status >= 500:
                raise self._unknown(write, "Amazon answered with a server error.")
            if not parsed_ok:
                raise self._unknown(write, "Amazon answered with a body that is not JSON.")
            return SpApiResponse(
                status=status, json=parsed, headers=dict(response.headers), raw=raw
            )
