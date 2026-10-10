"""The local fake marketplace (S15; RULES.md:128 stand-ins): an OAuth consent page and token
endpoint for Amazon (Login with Amazon) and Flipkart, so the connect flow runs end to end with no
real credentials and no network.

The app never imports this module and it is not in the image (`tests/` is not copied). Tests mount
it in process (`httpx.ASGITransport(build_fake(config))`); `make fake-marketplace` serves it on
127.0.0.1 for the e2e script.

- `GET /{mkt}/authorize`: one exact parameter set per marketplace (the real shapes are confirmed
  with the marketplaces at DW9). Amazon: `application_id`, `redirect_uri`, `state` (no PKCE).
  Flipkart: `client_id`, `redirect_uri`, `response_type=code`, `state`, `code_challenge`,
  `code_challenge_method=S256`. Two control parameters ride along: `seller=` picks the seller,
  `deny=1` answers `error=access_denied`. Any other or missing parameter is 400. It 302s to the
  redirect URI with a single-use code bound to that redirect URI, the seller and the challenge.
  Amazon calls back with `spapi_oauth_code`, `selling_partner_id` and `state`; Flipkart with
  `code` and `state`.
- `POST /{mkt}/token`, form-encoded: `authorization_code` checks the client credentials, the
  redirect URI, the single-use code and (Flipkart) the PKCE verifier; Amazon refuses a
  `code_verifier`. `refresh_token` returns a new access token and the same refresh token (as LWA).
  Token values carry the canary `CANARYzq9-<16 hex>`, which no log, body or row may ever hold.
- `POST /__control` (JSON): `next_refresh` (`ok|invalid_grant|server_error|timeout`),
  `next_exchange` (`ok|invalid_grant|server_error`), `next_exchange_seller`, `expires_in`. Each
  `next_*` applies once. `GET /healthz`.

SP-API Listings Items 2021-08-01 (S20 T9; the shapes are believed from the public docs and
confirmed at DW1): `PUT|PATCH|GET /listings/2021-08-01/items/{sellerId}/{sku}?marketplaceIds=`.
- `x-amz-access-token` must be a token this fake issued (an LWA grant or `issue_token`); any
  other is 403 `Unauthorized`.
- PUT stores the item per (seller, sku) and answers `{sku, status, submissionId, issues}`: an
  attribute with an ERROR issue is not stored; when every sent attribute is rejected, or the SKU
  is marked invalid, the status is `INVALID` and nothing is stored. PATCH applies `replace` ops
  to the named attributes only (404 for an unknown SKU). GET answers `summaries[{marketplaceId,
  asin, status}]`, the stored attributes and the last issues, or 404.
- `/__control` also takes: `spapi_issues {sku: [issue]}` (once per SKU), `spapi_reject
  {attribute: [code, message]}` (every SKU), `spapi_invalid [sku]`, `spapi_429 n`,
  `spapi_after_write {sku: status}` (apply, then answer that status, once), `spapi_auth
  {mode: expired|denied, times}` (expired -> 401, denied -> 403), `spapi_summary {sku:
  BUYABLE|DISCOVERABLE}`, `spapi_body redirect|oversize|html` (the next SP-API answer, once).
- `GET /__calls`: every SP-API call (method, path, time, body sha256).
- S26 (failure injection): `spapi_body` also takes `malformed_json` (a truncated JSON reply) and
  `extra_field` (the real reply plus `"unexpected": 1`); `spapi_drop_after_write [sku]` stores the
  write, then drops the connection (`httpx.RemoteProtocolError` in process: no reply at all);
  `next_refresh` also takes `malformed`, `oversize` and `extra_field`, each reply embedding the
  `canary` string the control call passes (so a test can prove it is never echoed).
- S21 (CP15): `spapi_get {sku: buyable|discoverable|inactive|rejected|gone}` sets what a GET of the
  SKU reads until changed (gone = 404); `POST /__control/reset {"prefix": "<sku prefix>"}` forgets
  the items, issues, GET states and calls of SKUs with that prefix (an e2e run cleans up after
  itself without touching another run's SKUs).

Flipkart Seller API listings v3 (S22 T9; believed, DW1) under `/flipkart/sellers`:
`POST /listings/v3` (create, a batch), `POST /listings/v3/update` (named attributes only) and
`GET /listings/v3/{sku,sku,...}`. The `Authorization: Bearer` token must be one this fake issued
(an OAuth grant or `issue_token`), else 401. Items are stored per (seller, sku); an attribute with
an ERROR is not stored, and a SKU whose every attribute is refused, or that is marked rejected,
is `FAILURE` and stores nothing. Answers `{request_id, results: [{sku, status, errors}]}`; an
update of an unknown SKU is `FAILURE` with `LISTING_NOT_FOUND`. GET lists the stored SKUs only.
- `/__control` also takes: `fk_errors {sku: {attribute: code}}` (once per SKU),
  `fk_warnings {sku: {attribute: code}}` (once), `fk_reject_attribute {attribute: [code, text]}`
  (every SKU), `fk_reject [sku]`, `fk_429 n`, `fk_after_write status` (apply, then answer that
  status, once), `fk_auth {mode: expired|denied, times}`, `fk_state {sku: ACTIVE|INACTIVE}`,
  `fk_body redirect|oversize|html` (once), `reset_calls`.
- `GET /__calls?marketplace=flipkart|amazon`: `{count, calls}` of that marketplace only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

CANARY: Final = "CANARYzq9"
CONTROL_PARAMS: Final = frozenset({"seller", "deny"})
AMAZON_PARAMS: Final = frozenset({"application_id", "redirect_uri", "state"})
FLIPKART_PARAMS: Final = frozenset(
    {
        "client_id",
        "redirect_uri",
        "response_type",
        "state",
        "code_challenge",
        "code_challenge_method",
    }
)


@dataclass(frozen=True)
class FakeApp:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class FakeConfig:
    """The credentials the fake accepts. The defaults are the `.env.example` placeholders and the
    frozen local redirect URIs (CANON.md:37-38)."""

    amazon: FakeApp = FakeApp(
        "amzn1.application-oa2-client.REPLACE_ME",
        "REPLACE_ME",
        "http://localhost:8000/api/v1/marketplaces/amazon/callback",
    )
    flipkart: FakeApp = FakeApp(
        "REPLACE_ME",
        "REPLACE_ME",
        "http://localhost:8000/api/v1/marketplaces/flipkart/callback",
    )
    amazon_application_id: str = "amzn1.sp.solution.REPLACE_ME"
    default_seller: str = "A1FAKESELLER"
    expires_in: int = 3600
    timeout_s: float = 15.0


@dataclass
class _Grant:
    marketplace: str
    redirect_uri: str
    seller: str
    challenge: str | None


@dataclass
class FakeState:
    codes: dict[str, _Grant] = field(default_factory=dict)
    refresh_tokens: dict[str, tuple[str, str]] = field(default_factory=dict)  # -> (mkt, seller)
    next_refresh: str = "ok"
    next_exchange: str = "ok"
    next_exchange_seller: str | None = None
    expires_in: int | None = None
    exchanges: int = 0
    refreshes: int = 0
    token_requests: list[dict[str, str]] = field(default_factory=list)
    # ---- SP-API (S20 T9) ----
    access_tokens: set[str] = field(default_factory=set)
    spapi_items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    spapi_issues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    spapi_reject: dict[str, tuple[str, str]] = field(default_factory=dict)
    spapi_invalid: set[str] = field(default_factory=set)
    spapi_429: int = 0
    spapi_after_write: dict[str, int] = field(default_factory=dict)
    spapi_auth_mode: str | None = None
    spapi_auth_times: int = 0
    spapi_summary: dict[str, str] = field(default_factory=dict)
    spapi_body: str | None = None
    spapi_get: dict[str, str] = field(default_factory=dict)
    # ---- S26 failure injection ----
    spapi_drop_after_write: set[str] = field(default_factory=set)
    refresh_canary: str = "CANARYzq9-0000000000000000"
    calls: list[dict[str, Any]] = field(default_factory=list)
    submissions: int = 0
    # ---- Flipkart listings v3 (S22 T9) ----
    access_sellers: dict[str, str] = field(default_factory=dict)
    fk_items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    fk_errors: dict[str, dict[str, str]] = field(default_factory=dict)
    fk_warnings: dict[str, dict[str, str]] = field(default_factory=dict)
    fk_reject_attribute: dict[str, tuple[str, str]] = field(default_factory=dict)
    fk_reject: set[str] = field(default_factory=set)
    fk_429: int = 0
    fk_after_write: int | None = None
    fk_auth_mode: str | None = None
    fk_auth_times: int = 0
    fk_state: dict[str, str] = field(default_factory=dict)
    fk_body: str | None = None
    fk_requests: int = 0


def canary_token() -> str:
    return f"{CANARY}-{secrets.token_hex(8)}"


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _bad(reason: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status)


def build_fake(config: FakeConfig | None = None) -> Starlette:
    cfg = config or FakeConfig()
    state = FakeState()
    apps = {"amazon": cfg.amazon, "flipkart": cfg.flipkart}

    def _app(request: Request) -> tuple[str, FakeApp] | None:
        mkt = request.path_params["mkt"]
        return (mkt, apps[mkt]) if mkt in apps else None

    async def authorize(request: Request) -> Response:
        found = _app(request)
        if found is None:
            return _bad("unknown marketplace", 404)
        mkt, app = found
        params = request.query_params
        if len(params.keys()) != len(params.multi_items()):
            return _bad("repeated parameter")
        names = set(params.keys()) - CONTROL_PARAMS
        expected = AMAZON_PARAMS if mkt == "amazon" else FLIPKART_PARAMS
        if names != expected:
            return _bad("wrong parameter set")
        if params["redirect_uri"] != app.redirect_uri or not params["state"]:
            return _bad("redirect_uri mismatch")
        if mkt == "amazon":
            if params["application_id"] != cfg.amazon_application_id:
                return _bad("unknown application_id")
        else:
            if params["client_id"] != app.client_id or params["response_type"] != "code":
                return _bad("unknown client")
            if params["code_challenge_method"] != "S256" or len(params["code_challenge"]) != 43:
                return _bad("S256 PKCE required")
        if params.get("deny") == "1":
            query = {"error": "access_denied", "state": params["state"]}
            return RedirectResponse(f"{app.redirect_uri}?{urlencode(query)}", status_code=302)
        seller = params.get("seller", cfg.default_seller)
        code = f"code-{secrets.token_hex(12)}"
        challenge = params.get("code_challenge") if mkt == "flipkart" else None
        state.codes[code] = _Grant(mkt, app.redirect_uri, seller, challenge)
        if mkt == "amazon":
            query = {
                "spapi_oauth_code": code,
                "selling_partner_id": seller,
                "state": params["state"],
            }
        else:
            query = {"code": code, "state": params["state"]}
        return RedirectResponse(f"{app.redirect_uri}?{urlencode(query)}", status_code=302)

    def _tokens(mkt: str, seller: str, refresh: str | None = None) -> dict[str, Any]:
        expires_in = state.expires_in if state.expires_in is not None else cfg.expires_in
        refresh_token = refresh or canary_token()
        state.refresh_tokens[refresh_token] = (mkt, seller)
        access = canary_token()
        state.access_tokens.add(access)
        state.access_sellers[access] = seller
        body: dict[str, Any] = {
            "access_token": access,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": "sellingpartnerapi::listings" if mkt == "amazon" else "Seller_Api",
        }
        if mkt == "flipkart":
            body["seller_id"] = seller
        return body

    async def _exchange(mkt: str, app: FakeApp, form: dict[str, str]) -> Response:
        state.exchanges += 1
        outcome, state.next_exchange = state.next_exchange, "ok"
        if outcome == "server_error":
            return _bad("server_error", 500)
        grant = state.codes.pop(form.get("code", ""), None)
        if outcome == "invalid_grant" or grant is None or grant.marketplace != mkt:
            return _bad("invalid_grant")
        if form.get("redirect_uri") != grant.redirect_uri:
            return _bad("invalid_grant")
        verifier = form.get("code_verifier")
        if mkt == "amazon":
            if verifier is not None:
                return _bad("invalid_request")
        elif verifier is None or grant.challenge is None or s256(verifier) != grant.challenge:
            return _bad("invalid_grant")
        seller, state.next_exchange_seller = (
            state.next_exchange_seller or grant.seller,
            None,
        )
        return JSONResponse(_tokens(mkt, seller))

    async def _refresh(mkt: str, form: dict[str, str]) -> Response:
        state.refreshes += 1
        outcome, state.next_refresh = state.next_refresh, "ok"
        if outcome == "server_error":
            return _bad("server_error", 500)
        if outcome == "timeout":
            await asyncio.sleep(cfg.timeout_s)
        canary = state.refresh_canary
        if outcome == "malformed":
            return Response(
                f'{{"access_token": "{canary}", "token_ty', media_type="application/json"
            )
        if outcome == "oversize":
            pad = "x" * OVERSIZE_BYTES
            return JSONResponse({"access_token": canary, "token_type": "bearer", "pad": pad})
        known = state.refresh_tokens.get(form.get("refresh_token", ""))
        if outcome == "invalid_grant" or known is None or known[0] != mkt:
            return _bad("invalid_grant")
        body = _tokens(mkt, known[1], refresh=form["refresh_token"])
        if mkt == "flipkart":
            body.pop("seller_id", None)
        if outcome == "extra_field":
            body["unexpected"] = canary
        return JSONResponse(body)

    async def token(request: Request) -> Response:
        found = _app(request)
        if found is None:
            return _bad("unknown marketplace", 404)
        mkt, app = found
        if request.headers.get("content-type", "").split(";")[0] != (
            "application/x-www-form-urlencoded"
        ):
            return _bad("invalid_request")
        form = {k: str(v) for k, v in (await request.form()).items()}
        state.token_requests.append({"marketplace": mkt, **form})
        if form.get("client_id") != app.client_id or form.get("client_secret") != (
            app.client_secret
        ):
            return _bad("invalid_client", 401)
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            return await _exchange(mkt, app, form)
        if grant_type == "refresh_token":
            return await _refresh(mkt, form)
        return _bad("unsupported_grant_type")

    async def control(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict):
            return _bad("expected an object")
        if body.get("next_refresh") in REFRESH_OUTCOMES:
            state.next_refresh = body["next_refresh"]
        if isinstance(body.get("canary"), str):
            state.refresh_canary = body["canary"]
        if body.get("next_exchange") in ("ok", "invalid_grant", "server_error"):
            state.next_exchange = body["next_exchange"]
        if isinstance(body.get("next_exchange_seller"), str):
            state.next_exchange_seller = body["next_exchange_seller"]
        if "expires_in" in body:
            state.expires_in = body["expires_in"]
        _spapi_control(state, body)
        _flipkart_control(state, body)
        return JSONResponse({"ok": True})

    async def reset(request: Request) -> Response:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("prefix"), str):
            return _bad("expected {prefix}")
        return JSONResponse({"reset": reset_prefix(state, body["prefix"])})

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def spapi(request: Request) -> Response:
        return await _spapi(state, request)

    async def calls(request: Request) -> Response:
        marketplace = request.query_params.get("marketplace")
        if marketplace is None:
            return JSONResponse(state.calls)
        picked = [c for c in state.calls if c.get("marketplace", "amazon") == marketplace]
        return JSONResponse({"count": len(picked), "calls": picked})

    async def flipkart(request: Request) -> Response:
        return await _flipkart(state, request)

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/__control", control, methods=["POST"]),
            Route("/__control/reset", reset, methods=["POST"]),
            Route("/__calls", calls, methods=["GET"]),
            Route(SPAPI_ROUTE, spapi, methods=["GET", "PUT", "PATCH"]),
            Route(FK_CREATE_ROUTE, flipkart, methods=["POST"]),
            Route(FK_UPDATE_ROUTE, flipkart, methods=["POST"]),
            Route(FK_GET_ROUTE, flipkart, methods=["GET"]),
            Route("/{mkt}/authorize", authorize, methods=["GET"]),
            Route("/{mkt}/token", token, methods=["POST"]),
        ]
    )
    app.state.fake = state
    app.state.config = cfg
    return app


# ---- SP-API Listings Items (S20 T9) ----

SPAPI_ROUTE: Final = "/listings/2021-08-01/items/{seller}/{sku:path}"
OVERSIZE_BYTES: Final = 2 * 1024 * 1024
REFRESH_OUTCOMES: Final = frozenset(
    {"ok", "invalid_grant", "server_error", "timeout", "malformed", "oversize", "extra_field"}
)
SPAPI_BODIES: Final = frozenset({"redirect", "oversize", "html", "malformed_json", "extra_field"})


def issue_token(app: Starlette) -> str:
    """A fresh access token the fake accepts (tests that skip the LWA flow)."""
    token = canary_token()
    app.state.fake.access_tokens.add(token)
    return token


def _spapi_control(state: FakeState, body: dict[str, Any]) -> None:
    if isinstance(body.get("spapi_issues"), dict):
        state.spapi_issues.update(body["spapi_issues"])
    if isinstance(body.get("spapi_reject"), dict):
        state.spapi_reject.update({k: (v[0], v[1]) for k, v in body["spapi_reject"].items()})
    if isinstance(body.get("spapi_invalid"), list):
        state.spapi_invalid |= set(body["spapi_invalid"])
    if isinstance(body.get("spapi_429"), int):
        state.spapi_429 = body["spapi_429"]
    if isinstance(body.get("spapi_after_write"), dict):
        state.spapi_after_write.update(body["spapi_after_write"])
    auth = body.get("spapi_auth")
    if isinstance(auth, dict) and auth.get("mode") in ("expired", "denied"):
        state.spapi_auth_mode, state.spapi_auth_times = auth["mode"], int(auth.get("times", 1))
    if isinstance(body.get("spapi_summary"), dict):
        state.spapi_summary.update(body["spapi_summary"])
    if body.get("spapi_body") in SPAPI_BODIES:
        state.spapi_body = body["spapi_body"]
    if isinstance(body.get("spapi_drop_after_write"), list):
        state.spapi_drop_after_write |= {str(sku) for sku in body["spapi_drop_after_write"]}
    gets = body.get("spapi_get")
    if isinstance(gets, dict):
        states = ("buyable", "discoverable", "inactive", "rejected", "gone")
        state.spapi_get.update({k: v for k, v in gets.items() if v in states})


def reset_prefix(state: FakeState, prefix: str) -> int:
    """Forget every SP-API item, issue, GET state and call of SKUs starting with `prefix`."""
    if not prefix:
        return 0
    gone = [key for key in state.spapi_items if key[1].startswith(prefix)]
    for key in gone:
        del state.spapi_items[key]
    for table in (state.spapi_issues, state.spapi_get, state.spapi_summary):
        for sku in [s for s in table if s.startswith(prefix)]:
            del table[sku]
    state.calls = [c for c in state.calls if f"/{prefix}" not in c["path"]]
    return len(gone)


def _errors(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"errors": [{"code": code, "message": message}]}, status_code=status)


def _issues_for(state: FakeState, sku: str, attributes: dict[str, Any]) -> list[dict[str, Any]]:
    issues = list(state.spapi_issues.pop(sku, []))
    for name in sorted(attributes):
        if name in state.spapi_reject:
            code, message = state.spapi_reject[name]
            issues.append(
                {
                    "code": code,
                    "message": message,
                    "severity": "ERROR",
                    "attributeNames": [name],
                    "categories": ["INVALID_ATTRIBUTE"],
                }
            )
    return issues


def _rejected(issues: list[dict[str, Any]]) -> set[str]:
    return {
        name
        for issue in issues
        if issue.get("severity") == "ERROR"
        for name in issue.get("attributeNames", [])
    }


async def _spapi(state: FakeState, request: Request) -> Response:
    raw = await request.body()
    seller, sku = request.path_params["seller"], request.path_params["sku"]
    state.calls.append(
        {
            "marketplace": "amazon",
            "method": request.method,
            "path": request.url.path,
            "raw_path": request.scope.get("raw_path", b"").decode("latin-1"),
            "time": time.monotonic(),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    if request.query_params.get("marketplaceIds") is None:
        return _errors("InvalidInput", "marketplaceIds is required", 400)
    token = request.headers.get("x-amz-access-token", "")
    if state.spapi_auth_mode is not None and state.spapi_auth_times > 0:
        state.spapi_auth_times -= 1
        mode = state.spapi_auth_mode
        if state.spapi_auth_times == 0:
            state.spapi_auth_mode = None
        if mode == "expired":
            return _errors("Unauthorized", "The access token you provided has expired.", 401)
        return _errors("Unauthorized", "Access to requested resource is denied.", 403)
    if token not in state.access_tokens:
        return _errors("Unauthorized", "Access to requested resource is denied.", 403)
    if state.spapi_429 > 0:
        state.spapi_429 -= 1
        return _errors("QuotaExceeded", "You exceeded your quota for the requested resource.", 429)
    shaped = state.spapi_body
    state.spapi_body = None
    if shaped == "redirect":
        # a same-host SP-API path, so a client that follows it shows up in `/__calls`
        target = f"/listings/2021-08-01/items/{seller}/REDIRECTED?marketplaceIds=A21TJRUUN4KGV"
        return RedirectResponse(target, status_code=302)
    if shaped == "html":
        return Response("<html>maintenance</html>", status_code=200, media_type="text/html")
    if shaped == "oversize":
        filler = "x" * OVERSIZE_BYTES
        return JSONResponse({"sku": sku, "status": "ACCEPTED", "pad": filler})
    if shaped == "malformed_json":
        return Response(f'{{"sku": "{sku}", "status": "ACC', media_type="application/json")
    key = (seller, sku)
    if request.method == "GET":
        return _spapi_get(state, key)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _errors("InvalidInput", "body is not JSON", 400)
    if request.method == "PATCH":
        response = _spapi_patch(state, key, body)
    else:
        response = _spapi_put(state, key, body)
    after = state.spapi_after_write.pop(sku, None)
    if after is not None:
        return _errors("InternalFailure", "We encountered an internal error.", after)
    if sku in state.spapi_drop_after_write:
        state.spapi_drop_after_write.discard(sku)
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
    if shaped == "extra_field":
        reply = json.loads(bytes(response.body))
        reply["unexpected"] = 1
        return JSONResponse(reply, status_code=response.status_code)
    return response


def _accepted(state: FakeState, sku: str, issues: list[dict[str, Any]], status: str) -> Response:
    state.submissions += 1
    return JSONResponse(
        {
            "sku": sku,
            "status": status,
            "submissionId": f"fake-submission-{state.submissions:06d}",
            "issues": issues,
        }
    )


def _spapi_put(state: FakeState, key: tuple[str, str], body: dict[str, Any]) -> Response:
    sku = key[1]
    attributes = body.get("attributes")
    if not isinstance(attributes, dict) or not isinstance(body.get("productType"), str):
        return _errors("InvalidInput", "productType and attributes are required", 400)
    issues = _issues_for(state, sku, attributes)
    rejected = _rejected(issues)
    sent = set(attributes)
    if sku in state.spapi_invalid or (sent and sent <= rejected):
        return _accepted(state, sku, issues, "INVALID")
    state.spapi_items[key] = {
        "productType": body["productType"],
        "attributes": {k: v for k, v in attributes.items() if k not in rejected},
        "issues": issues,
    }
    return _accepted(state, sku, issues, "ACCEPTED")


def _spapi_patch(state: FakeState, key: tuple[str, str], body: dict[str, Any]) -> Response:
    stored = state.spapi_items.get(key)
    if stored is None:
        return _errors("NOT_FOUND", "The requested listing was not found.", 404)
    patches = body.get("patches")
    if not isinstance(patches, list) or not patches:
        return _errors("InvalidInput", "patches are required", 400)
    named: dict[str, Any] = {}
    for patch in patches:
        path = patch.get("path", "")
        if patch.get("op") != "replace" or not path.startswith("/attributes/"):
            return _errors("InvalidInput", "only replace of /attributes/<name>", 400)
        named[path.removeprefix("/attributes/")] = patch.get("value")
    issues = _issues_for(state, key[1], named)
    rejected = _rejected(issues)
    if key[1] in state.spapi_invalid or set(named) <= rejected:
        return _accepted(state, key[1], issues, "INVALID")
    stored["attributes"].update({k: v for k, v in named.items() if k not in rejected})
    stored["issues"] = issues
    return _accepted(state, key[1], issues, "ACCEPTED")


def _spapi_get(state: FakeState, key: tuple[str, str]) -> Response:
    stored = state.spapi_items.get(key)
    mode = state.spapi_get.get(key[1])
    if stored is None or mode == "gone":
        return _errors("NOT_FOUND", "The requested listing was not found.", 404)
    status = state.spapi_summary.get(key[1], "DISCOVERABLE")
    issues = stored["issues"]
    if mode == "buyable":
        status = "BUYABLE"
    elif mode == "discoverable":
        status, issues = "DISCOVERABLE", []
    elif mode == "rejected":
        status = "DISCOVERABLE"
        issues = [{"code": "90220", "message": "The listing was rejected.", "severity": "ERROR"}]
    statuses: list[str] = (
        []
        if mode == "inactive"
        else [status] if status == "DISCOVERABLE" else [status, "DISCOVERABLE"]
    )
    return JSONResponse(
        {
            "sku": key[1],
            "summaries": [
                {
                    "marketplaceId": "A21TJRUUN4KGV",
                    "asin": "B0" + hashlib.sha256("/".join(key).encode()).hexdigest()[:8].upper(),
                    "productType": stored["productType"],
                    "status": statuses,
                }
            ],
            "attributes": stored["attributes"],
            "issues": issues,
        }
    )


# ---- Flipkart listings v3 (S22 T9; believed, DW1) ----

FK_BASE: Final = "/flipkart/sellers"
FK_CREATE_ROUTE: Final = f"{FK_BASE}/listings/v3"
FK_UPDATE_ROUTE: Final = f"{FK_BASE}/listings/v3/update"
FK_GET_ROUTE: Final = f"{FK_BASE}/listings/v3/{{sku_ids}}"


def _flipkart_control(state: FakeState, body: dict[str, Any]) -> None:
    for name in ("fk_errors", "fk_warnings"):
        value = body.get(name)
        if isinstance(value, dict):
            getattr(state, name).update({k: dict(v) for k, v in value.items()})
    if isinstance(body.get("fk_reject_attribute"), dict):
        state.fk_reject_attribute.update(
            {k: (v[0], v[1]) for k, v in body["fk_reject_attribute"].items()}
        )
    if isinstance(body.get("fk_reject"), list):
        state.fk_reject |= set(body["fk_reject"])
    if isinstance(body.get("fk_429"), int):
        state.fk_429 = body["fk_429"]
    if isinstance(body.get("fk_after_write"), int):
        state.fk_after_write = body["fk_after_write"]
    auth = body.get("fk_auth")
    if isinstance(auth, dict) and auth.get("mode") in ("expired", "denied"):
        state.fk_auth_mode, state.fk_auth_times = auth["mode"], int(auth.get("times", 1))
    if isinstance(body.get("fk_state"), dict):
        state.fk_state.update(body["fk_state"])
    if body.get("fk_body") in ("redirect", "oversize", "html"):
        state.fk_body = body["fk_body"]
    if body.get("reset_calls") is True:
        state.calls.clear()


def _fk_error(code: str, description: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"errors": [{"code": code, "description": description, "severity": "ERROR"}]},
        status_code=status,
    )


def _fk_shaped(state: FakeState) -> Response | None:
    shaped, state.fk_body = state.fk_body, None
    if shaped == "redirect":
        return RedirectResponse(f"{FK_BASE}/listings/v3/REDIRECTED", status_code=302)
    if shaped == "html":
        return Response("<html>maintenance</html>", status_code=200, media_type="text/html")
    if shaped == "oversize":
        return JSONResponse({"request_id": "x", "results": [], "pad": "x" * OVERSIZE_BYTES})
    return None


def _fk_auth(state: FakeState, request: Request) -> Response | str:
    """The seller of a token this fake issued, or the 401/403 answer."""
    if state.fk_auth_mode is not None and state.fk_auth_times > 0:
        state.fk_auth_times -= 1
        mode = state.fk_auth_mode
        if state.fk_auth_times == 0:
            state.fk_auth_mode = None
        if mode == "expired":
            return _fk_error("UNAUTHORIZED", "The access token has expired.", 401)
        return _fk_error("FORBIDDEN", "The seller did not grant this application.", 403)
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token not in state.access_tokens:
        return _fk_error("UNAUTHORIZED", "Invalid access token.", 401)
    return state.access_sellers.get(token, "FKDEFAULT")


def _fk_issues(state: FakeState, sku: str, attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for name, code in sorted(state.fk_errors.pop(sku, {}).items()):
        issues.append(
            {
                "code": code,
                "description": f"{name} is invalid.",
                "severity": "ERROR",
                "attribute": name,
            }
        )
    for name, code in sorted(state.fk_warnings.pop(sku, {}).items()):
        issues.append(
            {
                "code": code,
                "description": f"{name} looks odd.",
                "severity": "WARNING",
                "attribute": name,
            }
        )
    for name in sorted(attributes):
        if name in state.fk_reject_attribute:
            code, text = state.fk_reject_attribute[name]
            issues.append(
                {"code": code, "description": text, "severity": "ERROR", "attribute": name}
            )
    return issues


def _fk_write(state: FakeState, seller: str, entry: Any, *, update: bool) -> dict[str, Any]:
    if not isinstance(entry, dict) or not isinstance(entry.get("sku"), str):
        return {
            "sku": None,
            "status": "FAILURE",
            "errors": [
                {"code": "INVALID_REQUEST", "description": "sku is required", "severity": "ERROR"}
            ],
        }
    sku = entry["sku"]
    attributes = entry.get("attributes")
    key = (seller, sku)
    if not isinstance(attributes, dict) or (not update and not entry.get("vertical")):
        return {
            "sku": sku,
            "status": "FAILURE",
            "errors": [
                {
                    "code": "INVALID_REQUEST",
                    "description": "vertical and attributes are required",
                    "severity": "ERROR",
                }
            ],
        }
    if update and key not in state.fk_items:
        return {
            "sku": sku,
            "status": "FAILURE",
            "errors": [
                {
                    "code": "LISTING_NOT_FOUND",
                    "description": "No listing for this SKU.",
                    "severity": "ERROR",
                }
            ],
        }
    issues = _fk_issues(state, sku, attributes)
    refused = {i["attribute"] for i in issues if i["severity"] == "ERROR" and i.get("attribute")}
    if sku in state.fk_reject:
        issues.append(
            {"code": "FK-REJECTED", "description": "The listing was rejected.", "severity": "ERROR"}
        )
        return {"sku": sku, "status": "FAILURE", "errors": issues}
    if attributes and set(attributes) <= refused:
        return {"sku": sku, "status": "FAILURE", "errors": issues}
    kept = {k: v for k, v in attributes.items() if k not in refused}
    if update:
        state.fk_items[key]["attributes"].update(kept)
        state.fk_items[key]["errors"] = [i for i in issues if i["severity"] == "ERROR"]
    else:
        state.fk_items[key] = {
            "vertical": entry["vertical"],
            "attributes": kept,
            "errors": [i for i in issues if i["severity"] == "ERROR"],
            "listing_id": "LST" + hashlib.sha256("/".join(key).encode()).hexdigest()[:12].upper(),
            "fsn": "FSN" + hashlib.sha256(sku.encode()).hexdigest()[:13].upper(),
        }
    stored = state.fk_items[key]
    return {
        "sku": sku,
        "status": "SUCCESS",
        "listing_id": stored["listing_id"],
        "errors": issues,
    }


async def _flipkart(state: FakeState, request: Request) -> Response:
    raw = await request.body()
    state.calls.append(
        {
            "marketplace": "flipkart",
            "method": request.method,
            "path": request.url.path,
            "raw_path": request.scope.get("raw_path", b"").decode("latin-1"),
            "time": time.monotonic(),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
            "body": raw.decode("utf-8", "replace"),
        }
    )
    seller = _fk_auth(state, request)
    if isinstance(seller, Response):
        return seller
    if state.fk_429 > 0:
        state.fk_429 -= 1
        return _fk_error("RATE_LIMITED", "Too many requests.", 429)
    shaped = _fk_shaped(state)
    if shaped is not None:
        return shaped
    if request.method == "GET":
        skus = request.path_params["sku_ids"].split(",")
        found = []
        for sku in skus:
            stored = state.fk_items.get((seller, sku))
            if stored is not None:
                found.append(
                    {
                        "sku": sku,
                        "status": state.fk_state.get(sku, "INACTIVE"),
                        "fsn": stored["fsn"],
                        "listing_id": stored["listing_id"],
                        "attributes": stored["attributes"],
                        "errors": stored["errors"],
                    }
                )
        return JSONResponse({"listings": found})
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _fk_error("INVALID_REQUEST", "body is not JSON", 400)
    entries = body.get("listings") if isinstance(body, dict) else None
    if not isinstance(entries, list) or not entries:
        return _fk_error("INVALID_REQUEST", "listings are required", 400)
    update = request.url.path.endswith("/update")
    results = [_fk_write(state, seller, entry, update=update) for entry in entries]
    state.fk_requests += 1
    after, state.fk_after_write = state.fk_after_write, None
    if after is not None:
        return _fk_error("INTERNAL_ERROR", "Something went wrong.", after)
    return JSONResponse({"request_id": f"fk-req-{state.fk_requests:06d}", "results": results})


CONFIG_KEYS: Final = (
    "AMAZON_APP_ID",
    "AMAZON_CLIENT_ID",
    "AMAZON_CLIENT_SECRET",
    "AMAZON_OAUTH_REDIRECT_URI",
    "FLIPKART_CLIENT_ID",
    "FLIPKART_CLIENT_SECRET",
    "FLIPKART_OAUTH_REDIRECT_URI",
)


def _dotenv(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        found[key.strip()] = value.strip().strip("'\"")
    return found


def config_from_env(environ: Mapping[str, str], dotenv: Path) -> FakeConfig:
    """The credentials the local API uses, read the way the API reads them: the environment wins
    over `.env`; a blank or missing value keeps the fake's default. So the fake accepts exactly
    the client ids, secrets, redirect URIs and application id this checkout's API sends."""
    file_values = _dotenv(dotenv)
    values = {k: environ.get(k) or file_values.get(k) or "" for k in CONFIG_KEYS}
    base = FakeConfig()

    def pick(key: str, default: str) -> str:
        return values[key] or default

    return FakeConfig(
        amazon=FakeApp(
            pick("AMAZON_CLIENT_ID", base.amazon.client_id),
            pick("AMAZON_CLIENT_SECRET", base.amazon.client_secret),
            pick("AMAZON_OAUTH_REDIRECT_URI", base.amazon.redirect_uri),
        ),
        flipkart=FakeApp(
            pick("FLIPKART_CLIENT_ID", base.flipkart.client_id),
            pick("FLIPKART_CLIENT_SECRET", base.flipkart.client_secret),
            pick("FLIPKART_OAUTH_REDIRECT_URI", base.flipkart.redirect_uri),
        ),
        amazon_application_id=pick("AMAZON_APP_ID", base.amazon_application_id),
    )


def main(argv: list[str] | None = None) -> int:
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="The local fake marketplace (127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=4020)
    args = parser.parse_args(argv)
    config = config_from_env(os.environ, Path.cwd() / ".env")
    uvicorn.run(build_fake(config), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
