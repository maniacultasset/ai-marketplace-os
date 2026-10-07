"""S15 T8 (AC1-2, AC7-8; D4): the OAuth clients against the in-process fake marketplace (ASGI,
no network): the consent URL per marketplace, the exchange, the refresh and its three outcomes."""

from __future__ import annotations

import ast
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from tests.support.fake_marketplace import CANARY, FakeConfig, build_fake, s256

CFG = FakeConfig()
FAKE = "http://fake.marketplace.test"


def client_settings(**overrides: Any) -> Any:
    from app.core.config import Settings

    values: dict[str, Any] = {
        "app_env": "local",
        "app_role": "shell",
        "amazon_app_id": CFG.amazon_application_id,
        "amazon_client_id": CFG.amazon.client_id,
        "amazon_client_secret": CFG.amazon.client_secret,
        "amazon_authorize_url": f"{FAKE}/amazon/authorize",
        "amazon_lwa_token_url": f"{FAKE}/amazon/token",
        "flipkart_client_id": CFG.flipkart.client_id,
        "flipkart_client_secret": CFG.flipkart.client_secret,
        "flipkart_authorize_url": f"{FAKE}/flipkart/authorize",
        "flipkart_token_url": f"{FAKE}/flipkart/token",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def use_app(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    def install(app: Any) -> None:
        monkeypatch.setattr(
            "app.adapters.oauth.http.make_transport", lambda: httpx.ASGITransport(app=app)
        )

    return install


@pytest.fixture
def fake(use_app: Callable[[Any], None]) -> Iterator[Starlette]:
    app = build_fake(CFG)
    use_app(app)
    yield app


def _client(marketplace: str, **overrides: Any) -> Any:
    from app.adapters.oauth import get_oauth_client

    client = get_oauth_client(marketplace, client_settings(**overrides))
    assert client is not None
    return client


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


async def _consent(url: str, seller: str | None = None) -> dict[str, str]:
    """Follow the consent URL on the fake like a browser; the callback's query."""
    from app.adapters.oauth import http

    if seller:
        url = f"{url}&{urlencode({'seller': seller})}"
    async with http.oauth_http_client() as client:
        response = await client.get(url)
    assert response.status_code == 302, response.text
    return {k: v[0] for k, v in _params(response.headers["location"]).items()}


def test_authorize_url_params_flipkart() -> None:
    client = _client("flipkart")
    challenge = s256("v" * 64)
    params = _params(client.authorize_url(state="st-1", code_challenge=challenge))
    assert params == {
        "client_id": [CFG.flipkart.client_id],
        "redirect_uri": ["http://localhost:8000/api/v1/marketplaces/flipkart/callback"],
        "response_type": ["code"],
        "state": ["st-1"],
        "code_challenge": [challenge],
        "code_challenge_method": ["S256"],
    }
    assert client.uses_pkce is True


def test_authorize_url_params_amazon() -> None:
    client = _client("amazon")
    url = client.authorize_url(state="st-2", code_challenge=None)
    assert url.startswith(f"{FAKE}/amazon/authorize?")
    assert _params(url) == {
        "application_id": [CFG.amazon_application_id],
        "redirect_uri": ["http://localhost:8000/api/v1/marketplaces/amazon/callback"],
        "state": ["st-2"],
    }
    assert client.uses_pkce is False
    with pytest.raises(ValueError):
        client.authorize_url(state="st-2", code_challenge="c" * 43)


async def _exchange(marketplace: str, seller: str = "A7SELLER") -> Any:
    from app.adapters.oauth import CallbackParams

    client = _client(marketplace)
    verifier = secrets.token_urlsafe(48) if client.uses_pkce else None
    challenge = s256(verifier) if verifier else None
    query = await _consent(client.authorize_url(state="st", code_challenge=challenge), seller)
    code = query.get("code") or query["spapi_oauth_code"]
    callback = CallbackParams(code=code, selling_partner_id=query.get("selling_partner_id"))
    return await client.exchange(code_verifier=verifier, callback=callback)


@pytest.mark.parametrize("marketplace", ["amazon", "flipkart"])
async def test_exchange_ok_returns_grant(fake: Starlette, marketplace: str) -> None:
    grant = await _exchange(marketplace)
    assert grant.tokens.access_token.get_secret_value().startswith(f"{CANARY}-")
    assert grant.tokens.refresh_token.get_secret_value().startswith(f"{CANARY}-")
    assert grant.seller_id == "A7SELLER"
    assert grant.expires_at is not None
    assert grant.scopes


async def test_exchange_wrong_verifier_rejected(fake: Starlette) -> None:
    from app.adapters.oauth import CallbackParams, OAuthRejectedError

    client = _client("flipkart")
    challenge = s256(secrets.token_urlsafe(48))
    query = await _consent(client.authorize_url(state="st", code_challenge=challenge))
    with pytest.raises(OAuthRejectedError):
        await client.exchange(
            code_verifier=secrets.token_urlsafe(48), callback=CallbackParams(code=query["code"])
        )


async def test_exchange_amazon_sends_no_verifier(fake: Starlette) -> None:
    await _exchange("amazon")
    requests = [r for r in fake.state.fake.token_requests if r["marketplace"] == "amazon"]
    assert requests and all("code_verifier" not in r for r in requests)
    assert requests[-1]["grant_type"] == "authorization_code"


async def test_amazon_seller_from_param(fake: Starlette) -> None:
    grant = await _exchange("amazon", seller="A2PARAMSELLER")
    assert grant.seller_id == "A2PARAMSELLER"


@pytest.mark.parametrize(
    "seller",
    ["A1/B", "../x", "A1 B", "A" * 65, None],
    ids=["slash", "dotdot", "space", "65", "missing"],
)
async def test_flipkart_seller_pattern_protocol_error(
    fake: Starlette, use_app: Callable[[Any], None], seller: str | None
) -> None:
    from app.adapters.oauth import OAuthProtocolError

    if seller is not None:
        fake.state.fake.next_exchange_seller = seller
        with pytest.raises(OAuthProtocolError):
            await _exchange("flipkart")
        return

    async def no_seller(request: Request) -> Response:
        return JSONResponse(
            {"access_token": "a" * 10, "refresh_token": "r" * 10, "expires_in": 3600}
        )

    from app.adapters.oauth import CallbackParams

    use_app(Starlette(routes=[Route("/flipkart/token", no_seller, methods=["POST"])]))
    with pytest.raises(OAuthProtocolError):
        await _client("flipkart").exchange(code_verifier="v" * 43, callback=CallbackParams("c"))


async def _refresh_token(fake: Starlette) -> SecretStr:
    grant = await _exchange("amazon")
    return grant.tokens.refresh_token  # type: ignore[no-any-return]


async def test_refresh_ok(fake: Starlette) -> None:
    refresh = await _refresh_token(fake)
    grant = await _client("amazon").refresh(refresh)
    assert grant.tokens.access_token.get_secret_value().startswith(f"{CANARY}-")
    assert grant.tokens.refresh_token.get_secret_value() == refresh.get_secret_value()
    assert grant.expires_at is not None and grant.seller_id is None


async def test_refresh_invalid_grant_is_rejected(fake: Starlette) -> None:
    from app.adapters.oauth import OAuthRejectedError

    refresh = await _refresh_token(fake)
    fake.state.fake.next_refresh = "invalid_grant"
    with pytest.raises(OAuthRejectedError):
        await _client("amazon").refresh(refresh)


async def test_refresh_5xx_is_transient(fake: Starlette) -> None:
    from app.adapters.oauth import OAuthTransientError

    refresh = await _refresh_token(fake)
    fake.state.fake.next_refresh = "server_error"
    with pytest.raises(OAuthTransientError):
        await _client("amazon").refresh(refresh)


class _Raising(httpx.AsyncBaseTransport):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("refused"),
    ],
    ids=["read-timeout", "connect-timeout", "connect-error"],
)
async def test_refresh_timeout_is_transient(
    use_app: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    from app.adapters.oauth import OAuthTransientError

    monkeypatch.setattr("app.adapters.oauth.http.make_transport", lambda: _Raising(exc))
    with pytest.raises(OAuthTransientError) as info:
        await _client("amazon").refresh(SecretStr("Atzr|x"))
    assert info.value.__cause__ is None


async def test_bad_content_encoding_is_protocol_error(use_app: Callable[[Any], None]) -> None:
    """A 200 whose gzip body does not decode raises httpx.DecodingError (not a TransportError)
    while the body streams; it must map to OAuthProtocolError, never escape as httpx's own."""
    from app.adapters.oauth import OAuthProtocolError

    async def bad_gzip(request: Request) -> Response:
        return Response(
            b"not-gzip", media_type="application/json", headers={"Content-Encoding": "gzip"}
        )

    use_app(Starlette(routes=[Route("/amazon/token", bad_gzip, methods=["POST"])]))
    with pytest.raises(OAuthProtocolError) as info:
        await _client("amazon").refresh(SecretStr("r0"))
    assert info.value.__cause__ is None


async def test_other_httpx_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other httpx.HTTPError (a RequestError subclass outside the transport family) still
    lands on one of the three outcomes."""
    from app.adapters.oauth import OAuthTransientError

    monkeypatch.setattr(
        "app.adapters.oauth.http.make_transport",
        lambda: _Raising(httpx.RequestError("odd")),
    )
    with pytest.raises(OAuthTransientError) as info:
        await _client("amazon").refresh(SecretStr("r0"))
    assert info.value.__cause__ is None


async def test_invalid_client_is_protocol_error(fake: Starlette) -> None:
    from app.adapters.oauth import OAuthProtocolError

    refresh = await _refresh_token(fake)
    with pytest.raises(OAuthProtocolError):
        await _client("amazon", amazon_client_secret="wrong").refresh(refresh)


def _reply(body: Any, status: int = 200, raw: bytes | None = None) -> Starlette:
    async def handler(request: Request) -> Response:
        if raw is not None:
            return Response(raw, status_code=status, media_type="application/json")
        return JSONResponse(body, status_code=status)

    return Starlette(routes=[Route("/amazon/token", handler, methods=["POST"])])


@pytest.mark.parametrize("expires_in", [0, 86401, "NaN", "Infinity", "3600", 12.5, True], ids=str)
async def test_expires_in_bounds(use_app: Callable[[Any], None], expires_in: Any) -> None:
    from app.adapters.oauth import OAuthProtocolError

    if expires_in in ("NaN", "Infinity"):
        raw = (
            b'{"access_token": "a1", "refresh_token": "r1", "expires_in": '
            + expires_in.encode()
            + b"}"
        )
        use_app(_reply(None, raw=raw))
    else:
        use_app(_reply({"access_token": "a1", "refresh_token": "r1", "expires_in": expires_in}))
    with pytest.raises(OAuthProtocolError):
        await _client("amazon").refresh(SecretStr("r0"))


async def test_expires_in_upper_bound_accepted(use_app: Callable[[Any], None]) -> None:
    use_app(_reply({"access_token": "a1", "refresh_token": "r1", "expires_in": 86400}))
    grant = await _client("amazon").refresh(SecretStr("r0"))
    assert grant.expires_at is not None


async def test_oversized_body_protocol_error(use_app: Callable[[Any], None]) -> None:
    from app.adapters.oauth import OAuthProtocolError

    use_app(_reply({"access_token": "a1", "refresh_token": "r1", "pad": "x" * 70_000}))
    with pytest.raises(OAuthProtocolError):
        await _client("amazon").refresh(SecretStr("r0"))


async def test_oversized_chunked_body_protocol_error(use_app: Callable[[Any], None]) -> None:
    from app.adapters.oauth import OAuthProtocolError
    from starlette.responses import StreamingResponse

    async def endless(request: Request) -> Response:
        async def chunks() -> Any:
            for _ in range(200):
                yield b"x" * 1024

        return StreamingResponse(chunks(), media_type="application/json")

    use_app(Starlette(routes=[Route("/amazon/token", endless, methods=["POST"])]))
    with pytest.raises(OAuthProtocolError):
        await _client("amazon").refresh(SecretStr("r0"))


@pytest.mark.parametrize(
    "raw", [b"not json", b"[]", b'{"access_token": "a"}', b'{"access_token": 1}'], ids=str
)
async def test_unusable_reply_protocol_error(use_app: Callable[[Any], None], raw: bytes) -> None:
    from app.adapters.oauth import CallbackParams, OAuthProtocolError

    use_app(_reply(None, raw=raw))
    with pytest.raises(OAuthProtocolError):
        await _client("amazon").exchange(code_verifier=None, callback=CallbackParams("c", "A1"))


async def test_redirect_not_followed(use_app: Callable[[Any], None]) -> None:
    from app.adapters.oauth import OAuthProtocolError

    hits: list[str] = []

    async def redirect(request: Request) -> Response:
        return RedirectResponse("/amazon/elsewhere", status_code=307)

    async def elsewhere(request: Request) -> Response:
        hits.append("followed")
        return JSONResponse({"access_token": "a", "refresh_token": "r", "expires_in": 60})

    use_app(
        Starlette(
            routes=[
                Route("/amazon/token", redirect, methods=["POST"]),
                Route("/amazon/elsewhere", elsewhere, methods=["POST"]),
            ]
        )
    )
    with pytest.raises(OAuthProtocolError):
        await _client("amazon").refresh(SecretStr("r0"))
    assert hits == []


@pytest.mark.parametrize(
    ("marketplace", "unset"),
    [
        ("amazon", "amazon_client_id"),
        ("amazon", "amazon_client_secret"),
        ("amazon", "amazon_authorize_url"),
        ("amazon", "amazon_app_id"),
        ("flipkart", "flipkart_client_id"),
        ("flipkart", "flipkart_client_secret"),
        ("flipkart", "flipkart_authorize_url"),
    ],
)
def test_unconfigured_client_none(marketplace: str, unset: str) -> None:
    from app.adapters.oauth import get_oauth_client

    assert get_oauth_client(marketplace, client_settings(**{unset: None})) is None
    assert get_oauth_client(marketplace, client_settings(**{unset: "  "})) is None
    assert get_oauth_client("ebay", client_settings()) is None


async def test_errors_carry_no_body_or_token(use_app: Callable[[Any], None]) -> None:
    from app.adapters.oauth import OAuthError

    secret = f"{CANARY}-0123456789abcdef"
    for status, body in (
        (400, {"error": "invalid_grant", "error_description": secret}),
        (400, {"error": "invalid_client", "error_description": secret}),
        (503, {"detail": secret}),
        (200, {"access_token": secret, "expires_in": "x"}),
    ):
        use_app(_reply(body, status=status))
        with pytest.raises(OAuthError) as info:
            await _client("amazon").refresh(SecretStr(secret))
        rendered = str(info.value) + repr(info.value) + repr(info.value.args)
        assert CANARY not in rendered
        assert info.value.__cause__ is None


def test_client_repr_hides_secret() -> None:
    for marketplace in ("amazon", "flipkart"):
        client = _client(
            marketplace, amazon_client_secret="Zq9secret", flipkart_client_secret="Zq9s"
        )
        assert "Zq9" not in repr(client)


def test_client_module_does_not_import_credentials(repo_root: Path) -> None:
    for path in sorted((repo_root / "apps/api/app/adapters/oauth").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "app.adapters.credentials", path
                assert not (
                    node.module == "app.adapters"
                    and any(a.name == "credentials" for a in node.names)
                ), path
            if isinstance(node, ast.Import):
                assert all(a.name != "app.adapters.credentials" for a in node.names), path


def test_seller_pattern_matches_api_model() -> None:
    from app.adapters.oauth import SELLER_ID_RE
    from app.api.v1.schemas.marketplaces import SELLER_ID_PATTERN

    assert SELLER_ID_RE.pattern == SELLER_ID_PATTERN
