"""The fake Google identity provider (ADR-0020; RULES §8 stand-ins): an OpenID Connect consent page,
token endpoint and JWKS, so Google sign-in runs end to end with no real Google account and no
network. The app never imports it and it is not in the image.

It is mounted under `/google` by the fake marketplace (`build_fake`), so `make fake-marketplace`
serves it for the e2e run; tests mount `build_fake_google(cfg)` in process over ASGI.

- `GET /authorize`: exactly `client_id`, `redirect_uri`, `response_type=code`, `scope` (exactly
  `openid email profile`), `state`, `nonce`, `code_challenge`, `code_challenge_method=S256` and
  `prompt=select_account`, plus the control parameters `as` (the email that "signs in", default
  `asha.rao@gmail.com`), `hd` (a Workspace domain), `sub` (default: derived from the email, so
  the same email is the same Google account), `name`, `unverified=1` (`email_verified` false) and
  `deny=1` (answers `error=access_denied`). Any other, missing or repeated parameter is 400. It
  302s to the redirect URI with a single-use code bound to the redirect URI, the challenge, the
  nonce and the identity.
- `POST /token` (form): `grant_type=authorization_code`, the client id and secret, the code, the
  redirect URI and the PKCE verifier; the code works once. Answers an RS256 ID token signed with
  an in-memory key (`kid` `fake-1`), plus an access token holding the canary `CANARYzq9`, which
  the API never keeps. `invalid_grant` for a bad, used or mismatched code or verifier.
- `GET /certs`: the JWKS.
- `mint(claims)` signs any claims with the fake key (the verifier tests use it).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Final
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

KID: Final = "fake-1"
CANARY: Final = "CANARYzq9"
DEFAULT_EMAIL: Final = "asha.rao@gmail.com"
AUTHORIZE_PARAMS: Final = frozenset(
    {
        "client_id",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
        "nonce",
        "code_challenge",
        "code_challenge_method",
        "prompt",
    }
)
CONTROL_PARAMS: Final = frozenset({"as", "hd", "sub", "name", "unverified", "deny"})


@dataclass(frozen=True)
class FakeGoogleConfig:
    """What the fake accepts. The redirect URI is the local dashboard's (`DASHBOARD_BASE_URL`
    plus the API's callback path, ENV.md)."""

    client_id: str = "fake-google-client.apps.googleusercontent.com"
    client_secret: str = "fake-google-client-secret-local-only"
    redirect_uri: str = "http://localhost:3000/api/v1/auth/google/callback"
    issuer: str = "http://127.0.0.1:4020/google"
    token_ttl: int = 3600


@dataclass
class _Code:
    redirect_uri: str
    challenge: str
    claims: dict[str, Any]


@dataclass
class FakeGoogleState:
    codes: dict[str, _Code] = field(default_factory=dict)
    exchanges: int = 0
    jwks_requests: int = 0
    token_requests: list[dict[str, str]] = field(default_factory=list)


@cache
def signing_key() -> rsa.RSAPrivateKey:
    """One RSA key per test process (generating one is slow)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@cache
def other_key() -> rsa.RSAPrivateKey:
    """A second key, for the "signed by someone else" tests."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key: rsa.RSAPrivateKey | None = None, kid: str = KID) -> dict[str, Any]:
    public = jwt.algorithms.RSAAlgorithm.to_jwk((key or signing_key()).public_key(), as_dict=True)
    return {"keys": [{**public, "kid": kid, "use": "sig", "alg": "RS256"}]}


def mint(
    claims: dict[str, Any],
    *,
    key: rsa.RSAPrivateKey | None = None,
    kid: str = KID,
    algorithm: str = "RS256",
) -> str:
    return jwt.encode(claims, key or signing_key(), algorithm=algorithm, headers={"kid": kid})


def sub_for(email: str) -> str:
    """A stable, Google-shaped numeric subject for an email."""
    return str(int(hashlib.sha256(email.lower().encode()).hexdigest()[:16], 16))[:21]


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def claims_for(
    cfg: FakeGoogleConfig,
    *,
    email: str = DEFAULT_EMAIL,
    nonce: str,
    hd: str | None = None,
    sub: str | None = None,
    name: str | None = None,
    verified: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    moment = int(time.time() if now is None else now)
    display = name if name is not None else "Asha Rao"
    given, _, family = display.partition(" ")
    claims: dict[str, Any] = {
        "iss": cfg.issuer,
        "azp": cfg.client_id,
        "aud": cfg.client_id,
        "sub": sub or sub_for(email),
        "email": email,
        "email_verified": verified,
        "nonce": nonce,
        "name": display,
        "given_name": given,
        "family_name": family or None,
        "iat": moment,
        "exp": moment + cfg.token_ttl,
    }
    if hd is not None:
        claims["hd"] = hd
    return {k: v for k, v in claims.items() if v is not None}


def _bad(reason: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status)


def build_fake_google(config: FakeGoogleConfig | None = None) -> Starlette:
    cfg = config or FakeGoogleConfig()
    state = FakeGoogleState()

    async def authorize(request: Request) -> Response:
        params = request.query_params
        if len(params.keys()) != len(params.multi_items()):
            return _bad("repeated parameter")
        if set(params.keys()) - CONTROL_PARAMS != AUTHORIZE_PARAMS:
            return _bad("wrong parameter set")
        if params["client_id"] != cfg.client_id or params["redirect_uri"] != cfg.redirect_uri:
            return _bad("unknown client or redirect_uri")
        if params["response_type"] != "code" or params["scope"] != "openid email profile":
            return _bad("code flow with openid email profile only")
        if params["code_challenge_method"] != "S256" or len(params["code_challenge"]) != 43:
            return _bad("S256 PKCE required")
        if params["prompt"] != "select_account" or not params["state"] or not params["nonce"]:
            return _bad("state, nonce and prompt=select_account required")
        if params.get("deny") == "1":
            query = {"error": "access_denied", "state": params["state"]}
            return RedirectResponse(f"{cfg.redirect_uri}?{urlencode(query)}", status_code=302)
        claims = claims_for(
            cfg,
            email=params.get("as", DEFAULT_EMAIL),
            nonce=params["nonce"],
            hd=params.get("hd") or None,
            sub=params.get("sub") or None,
            name=params.get("name"),
            verified=params.get("unverified") != "1",
        )
        code = f"g-code-{secrets.token_hex(12)}"
        state.codes[code] = _Code(cfg.redirect_uri, params["code_challenge"], claims)
        query = {"code": code, "state": params["state"], "scope": "openid email profile"}
        return RedirectResponse(f"{cfg.redirect_uri}?{urlencode(query)}", status_code=302)

    async def token(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        state.token_requests.append(form)
        if form.get("grant_type") != "authorization_code":
            return _bad("unsupported_grant_type")
        if form.get("client_id") != cfg.client_id or form.get("client_secret") != cfg.client_secret:
            return _bad("invalid_client", 401)
        grant = state.codes.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != grant.redirect_uri:
            return _bad("invalid_grant")
        if s256(form.get("code_verifier", "")) != grant.challenge:
            return _bad("invalid_grant")
        state.exchanges += 1
        now = int(time.time())
        claims = {**grant.claims, "iat": now, "exp": now + cfg.token_ttl}
        return JSONResponse(
            {
                "access_token": f"{CANARY}-{secrets.token_hex(8)}",
                "expires_in": 3599,
                "scope": "openid email profile",
                "token_type": "Bearer",
                "id_token": mint(claims),
            }
        )

    async def certs(request: Request) -> Response:
        state.jwks_requests += 1
        return Response(json.dumps(jwks()), media_type="application/json")

    app = Starlette(
        routes=[
            Route("/authorize", authorize, methods=["GET"]),
            Route("/token", token, methods=["POST"]),
            Route("/certs", certs, methods=["GET"]),
        ]
    )
    app.state.fake = state
    app.state.config = cfg
    return app


__all__ = [
    "CANARY",
    "DEFAULT_EMAIL",
    "KID",
    "FakeGoogleConfig",
    "build_fake_google",
    "claims_for",
    "jwks",
    "mint",
    "other_key",
    "s256",
    "signing_key",
    "sub_for",
]
