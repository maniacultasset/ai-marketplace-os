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
- SI11: `put_preview` is the dry run (`mode=VALIDATION_PREVIEW`, which saves nothing). It has its
  own operation name and limiter bucket, and builds its own route: the mode is set by that route
  alone, no caller passes it, and `put` never carries one. A preview is not a write, so a reply
  it never got is `MarketplaceUnavailable` (retry the job), never ambiguous.
- DW2: one more route, `getDefinitionsProductType` (`get_json("ptd_get", ...)`, GET only, used by
  `app.adapters.amazon.definitions`). The product type must match `PRODUCT_TYPE_PATTERN` before
  the limiter or the token is touched, and the raw-path check holds for it too. A client built
  without a seller (`seller_id=None`, the platform PTD refresh) refuses every item call.
- One log line `marketplace.call` per try: operation, method, path template, duration, status,
  the rate-limit header, the first issue code and the try number. Never a URL, SKU, product
  type, body, header or token. The token is a `SecretStr` until the header is built.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote, urlencode

import httpx
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from pydantic import SecretStr

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
from app.adapters.telemetry import adapter_call_span
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
MAX_BODY: Final = 1024 * 1024
MAX_RETRIES_429: Final = 3
BACKOFF_CAP_S: Final = 60.0
JITTER_S: Final = 0.5
PATH_TEMPLATE: Final = f"{c.ITEMS_PATH}/{{sellerId}}/{{sku}}"
PTD_PATH_TEMPLATE: Final = f"{c.PTD_PATH}/{{productType}}"
PRODUCT_TYPE_RE: Final = re.compile(c.PRODUCT_TYPE_PATTERN)
ITEM_DENIED: Final = "The Amazon account did not grant the Listings role to this app."
PTD_DENIED: Final = "The Amazon app may not read product type definitions for this marketplace."
UNSAFE_SKUS: Final = frozenset({".", ".."})
GET_INCLUDED: Final = "summaries,attributes,issues"
PREVIEW_MODE: Final = "VALIDATION_PREVIEW"
# SI13: the read-only pre-check routes. Only ASCII digits for a barcode and an upper-case ASIN, so
# no other character (a full-width digit, a separator, a slash) ever reaches a query string.
BARCODE_RE: Final = re.compile(r"[0-9]{8,14}", re.ASCII)
ASIN_RE: Final = re.compile(r"[A-Z0-9]{10}", re.ASCII)
CATALOG_DENIED: Final = "The Amazon account did not grant the Product Listing role to this app."
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


_PAGE_TOKEN_MAX: Final = 2048
# marks a partial search whose next token was refused (not a credential)
UNFOLLOWED_TOKEN: Final = "unfollowed"  # noqa: S105


def _next_token(body: Mapping[str, Any]) -> str | None:
    """`pagination.nextToken` of a searchCatalogItems reply, or None (a bounded string only)."""
    pagination = body.get("pagination")
    token = pagination.get("nextToken") if isinstance(pagination, dict) else None
    if isinstance(token, str) and 0 < len(token) <= _PAGE_TOKEN_MAX and token.isprintable():
        return token
    return None


def _has_more(body: Mapping[str, Any]) -> bool:
    """True when the reply names any next page, even one `_next_token` refuses to follow."""
    pagination = body.get("pagination")
    return isinstance(pagination, dict) and bool(pagination.get("nextToken"))


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


@dataclass(frozen=True)
class _Route:
    """One built request target: what is sent, and what is logged (the template only)."""

    operation: str
    method: str
    path_template: str
    url: str
    raw_path: bytes
    write: bool
    denied_message: str


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
        seller_id: str | None,
        tokens: TokenSource,
        limiter: OutboundLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Rng | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_body: int = MAX_BODY,
    ) -> None:
        if seller_id is not None and (not seller_id or seller_id.strip() in UNSAFE_SKUS):
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
        self._used: list[SecretStr] = []
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

    def used_secrets(self) -> list[str]:
        """The token values this session sent, so the adapter can scrub them from any raw
        marketplace text it keeps (an error that echoes the token). Never logged (SI4 A16)."""
        return [s.get_secret_value() for s in self._used]

    def _remember(self, secret: SecretStr) -> None:
        value = secret.get_secret_value()
        if all(seen.get_secret_value() != value for seen in self._used):
            self._used.append(secret)

    # ---- the three calls ----

    async def put(self, sku: str, body: Mapping[str, Any]) -> SpApiResponse:
        return await self._call(self._item_route("put", "PUT", sku), body)

    async def patch(self, sku: str, body: Mapping[str, Any]) -> SpApiResponse:
        return await self._call(self._item_route("patch", "PATCH", sku), body)

    async def get(self, sku: str) -> SpApiResponse:
        return await self._call(self._item_route("get", "GET", sku), None)

    async def put_preview(self, sku: str, body: Mapping[str, Any]) -> SpApiResponse:
        """SI11: the dry run of a PUT. Amazon answers VALID or INVALID and stores nothing."""
        return await self._call(self._item_route("put_preview", "PUT", sku, preview=True), body)

    async def get_json(
        self,
        operation: Literal["ptd_get"],
        *,
        product_type: str,
        query: Mapping[str, str],
    ) -> SpApiResponse:
        """DW2: a read of the Product Type Definitions API (`app.adapters.amazon.definitions`)."""
        return await self._call(self._ptd_route(operation, product_type, query), None)

    async def search_catalog(
        self,
        identifiers: Sequence[str],
        identifiers_type: Literal["EAN", "UPC", "GTIN"],
    ) -> SpApiResponse:
        """SI13: searchCatalogItems by barcode (at most 20). Sends only the barcodes, their type,
        the marketplace and `pageSize=20`; refused before the limiter or a token when any value
        is not a plain 8-14 digit barcode. Follows `pagination.nextToken` (`pageToken`, each page
        through the limiter) for at most `CATALOG_MAX_PAGES` pages and merges the items. When a
        later page fails, the bound is hit or a next token is refused (too long, not printable),
        the merged reply keeps a `pagination.nextToken`, so the caller knows the search is partial; a failed first call raises as before.
        """
        values = list(identifiers) if not isinstance(identifiers, str) else []
        if not values or len(values) > c.CATALOG_MAX_IDENTIFIERS:
            raise ValueError("1 to 20 barcodes per search")
        if not all(isinstance(v, str) and BARCODE_RE.fullmatch(v) for v in values):
            raise ValueError("refused barcode")
        if identifiers_type not in ("EAN", "UPC", "GTIN"):
            raise ValueError("refused identifier type")
        query = {
            "identifiers": ",".join(values),
            "identifiersType": identifiers_type,
            "marketplaceIds": self._marketplace_id,
            "includedData": c.CATALOG_INCLUDED,
            "pageSize": str(c.CATALOG_PAGE_SIZE),
        }
        first = await self._call(self._read_route("catalog_search", c.CATALOG_PATH, query), None)
        body = first.json if isinstance(first.json, dict) else {}
        items = list(body.get("items") or []) if isinstance(body.get("items"), list) else []
        token = _next_token(body)
        unfollowed = token is None and _has_more(body)
        pages = 1
        while token is not None and pages < c.CATALOG_MAX_PAGES:
            route = self._read_route(
                "catalog_search", c.CATALOG_PATH, {**query, "pageToken": token}
            )
            try:
                page = await self._call(route, None)
            except AdapterError:
                break  # the token stays: the caller treats the search as incomplete
            found = page.json if isinstance(page.json, dict) else {}
            if page.status != 200 or not isinstance(found.get("items"), list):
                break
            items += found["items"]
            token = _next_token(found)
            unfollowed = token is None and _has_more(found)
            pages += 1
        merged: dict[str, Any] = {**body, "items": items}
        merged.pop("pagination", None)
        if token is not None:
            merged["pagination"] = {"nextToken": token}
        elif unfollowed:  # a next page the client will not follow: still a partial search
            merged["pagination"] = {"nextToken": UNFOLLOWED_TOKEN}
        return SpApiResponse(status=first.status, json=merged, headers=first.headers, raw=first.raw)

    async def get_restrictions(self, asin: str) -> SpApiResponse:
        """SI13: getListingsRestrictions for one ASIN and this seller (condition new)."""
        if not isinstance(asin, str) or not ASIN_RE.fullmatch(asin):
            raise ValueError("refused ASIN")
        if self._seller is None:
            raise ValueError("seller_id is required")
        query = {
            "asin": asin,
            "sellerId": self._seller,
            "marketplaceIds": self._marketplace_id,
            "conditionType": c.RESTRICTION_CONDITION,
        }
        route = self._read_route("restrictions_get", c.RESTRICTIONS_PATH, query)
        return await self._call(route, None)

    # ---- internals ----

    def _read_route(self, operation: str, path: str, query: Mapping[str, str]) -> _Route:
        """A GET with no body (`write=False`), checked raw-path-exact as every other route."""
        raw = f"{path}?{urlencode(dict(query), safe='')}"
        return _Route(
            operation, "GET", path, self._endpoint + raw, raw.encode("ascii"), False, CATALOG_DENIED
        )

    def _item_route(
        self, operation: str, method: str, sku: str, *, preview: bool = False
    ) -> _Route:
        if self._seller is None:
            raise ValueError("seller_id is required")
        if not isinstance(sku, str) or not sku or sku.strip() in UNSAFE_SKUS:
            raise ValueError("refused seller SKU")
        path = f"{c.ITEMS_PATH}/{quote(self._seller, safe='')}/{quote(sku, safe='')}"
        query: dict[str, str] = {"marketplaceIds": self._marketplace_id}
        if method == "GET":
            query["includedData"] = GET_INCLUDED
        if preview:
            query["mode"] = PREVIEW_MODE
        raw = f"{path}?{urlencode(query, safe='')}"
        return _Route(
            operation,
            method,
            PATH_TEMPLATE,
            self._endpoint + raw,
            raw.encode("ascii"),
            method != "GET" and not preview,
            ITEM_DENIED,
        )

    def _ptd_route(self, operation: str, product_type: str, query: Mapping[str, str]) -> _Route:
        if operation not in c.PTD_OPERATIONS:
            raise ValueError("unknown PTD operation")
        if not isinstance(product_type, str) or not PRODUCT_TYPE_RE.fullmatch(product_type):
            raise ValueError("refused product type")
        raw = f"{c.PTD_PATH}/{product_type}?{urlencode(dict(query), safe='')}"
        return _Route(
            operation,
            "GET",
            PTD_PATH_TEMPLATE,
            self._endpoint + raw,
            raw.encode("ascii"),
            False,
            PTD_DENIED,
        )

    def _request(self, route: _Route, content: bytes | None, token: str) -> httpx.Request:
        headers = {"x-amz-access-token": token, "accept": "application/json"}
        if content is not None:
            headers["content-type"] = "application/json"
        request = self._http.build_request(
            route.method, route.url, content=content, headers=headers
        )
        if request.url.raw_path != route.raw_path:  # C16: no dot segment or re-encoding
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

    def _log(self, route: _Route, started: float, status: int | None, **extra: Any) -> None:
        log.info(
            "marketplace.call",
            marketplace="amazon",
            operation=route.operation,
            method=route.method,
            path=route.path_template,
            duration_ms=round((self._clock() - started) * 1000, 1),
            status=status,
            **extra,
        )

    def _unknown(self, write: bool, message: str) -> AdapterError:
        if write:
            return MarketplaceAmbiguous(failure(ErrorCode.MARKETPLACE_UNCONFIRMED, message))
        return MarketplaceUnavailable(failure(ErrorCode.MARKETPLACE_UNAVAILABLE, message))

    async def _call(self, route: _Route, body: Mapping[str, Any] | None) -> SpApiResponse:
        # the route was built (and refused) before the limiter or the token is touched
        content = None
        if body is not None:
            content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        write = route.write
        retries = 0
        attempt = 0
        while True:
            attempt += 1
            with adapter_call_span(
                "amazon", route.operation, route.method, route.path_template
            ) as span:
                await self._limiter.acquire(route.operation)
                secret = await self._tokens.get()
                self._remember(secret)
                request = self._request(route, content, secret.get_secret_value())
                del secret
                started = self._clock()
                try:
                    response = await self._http.send(request, stream=True)
                except _BEFORE_SEND as exc:
                    self._log(route, started, None, attempt=attempt, error=type(exc).__name__)
                    raise MarketplaceUnavailable(
                        failure(ErrorCode.MARKETPLACE_UNAVAILABLE, "Amazon could not be reached.")
                    ) from None
                except httpx.HTTPError as exc:
                    self._log(route, started, None, attempt=attempt, error=type(exc).__name__)
                    raise self._unknown(
                        write, "Amazon did not answer the request in time."
                    ) from None
                try:
                    raw = await self._read(response)
                except _TooLarge:
                    self._log(route, started, response.status_code, attempt=attempt)
                    raise self._unknown(write, "Amazon answered with an oversized reply.") from None
                except httpx.HTTPError as exc:
                    self._log(
                        route,
                        started,
                        response.status_code,
                        attempt=attempt,
                        error=type(exc).__name__,
                    )
                    raise self._unknown(write, "Amazon's reply was cut off.") from None
                finally:
                    await response.aclose()
                status = response.status_code
                span.status(status)
                parsed: Any = None
                parsed_ok = True
                try:
                    parsed = json.loads(raw) if raw else {}
                except (ValueError, RecursionError):  # a deeply nested body is unreadable too
                    parsed_ok = False
                self._log(
                    route,
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
                        failure(ErrorCode.ACCOUNT_PERMISSION_DENIED, route.denied_message)
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
