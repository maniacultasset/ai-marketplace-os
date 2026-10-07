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
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

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
        body: dict[str, Any] = {
            "access_token": canary_token(),
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
        known = state.refresh_tokens.get(form.get("refresh_token", ""))
        if outcome == "invalid_grant" or known is None or known[0] != mkt:
            return _bad("invalid_grant")
        body = _tokens(mkt, known[1], refresh=form["refresh_token"])
        if mkt == "flipkart":
            body.pop("seller_id", None)
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
        if body.get("next_refresh") in ("ok", "invalid_grant", "server_error", "timeout"):
            state.next_refresh = body["next_refresh"]
        if body.get("next_exchange") in ("ok", "invalid_grant", "server_error"):
            state.next_exchange = body["next_exchange"]
        if isinstance(body.get("next_exchange_seller"), str):
            state.next_exchange_seller = body["next_exchange_seller"]
        if "expires_in" in body:
            state.expires_in = body["expires_in"]
        return JSONResponse({"ok": True})

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/__control", control, methods=["POST"]),
            Route("/{mkt}/authorize", authorize, methods=["GET"]),
            Route("/{mkt}/token", token, methods=["POST"]),
        ]
    )
    app.state.fake = state
    app.state.config = cfg
    return app


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
