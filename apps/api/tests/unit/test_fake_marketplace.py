"""S15 T7: the in-process fake marketplace (tests/support/fake_marketplace.py), over ASGI only."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from tests.support.fake_marketplace import CANARY, FakeConfig, build_fake, s256

CFG = FakeConfig()


@pytest.fixture
async def fake() -> AsyncIterator[httpx.AsyncClient]:
    app = build_fake(CFG)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


def _amazon_params(**extra: str) -> dict[str, str]:
    return {
        "application_id": CFG.amazon_application_id,
        "redirect_uri": CFG.amazon.redirect_uri,
        "state": "st-a",
        **extra,
    }


def _flipkart_params(verifier: str, **extra: str) -> dict[str, str]:
    return {
        "client_id": CFG.flipkart.client_id,
        "redirect_uri": CFG.flipkart.redirect_uri,
        "response_type": "code",
        "state": "st-f",
        "code_challenge": s256(verifier),
        "code_challenge_method": "S256",
        **extra,
    }


def _query(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


async def _token(fake: httpx.AsyncClient, mkt: str, **form: str) -> httpx.Response:
    app = CFG.amazon if mkt == "amazon" else CFG.flipkart
    body = {"client_id": app.client_id, "client_secret": app.client_secret, **form}
    return await fake.post(f"/{mkt}/token", data=body)


@pytest.mark.parametrize("mkt", ["amazon", "flipkart"])
async def test_fake_authorize_redirects_with_code_and_state(
    fake: httpx.AsyncClient, mkt: str
) -> None:
    verifier = secrets.token_urlsafe(48)
    params = _amazon_params(seller="A9SELL") if mkt == "amazon" else _flipkart_params(verifier)
    response = await fake.get(f"/{mkt}/authorize", params=params)
    query = _query(response)
    redirect = CFG.amazon.redirect_uri if mkt == "amazon" else CFG.flipkart.redirect_uri
    assert response.headers["location"].startswith(redirect + "?")
    if mkt == "amazon":
        assert set(query) == {"spapi_oauth_code", "selling_partner_id", "state"}
        assert query["selling_partner_id"] == "A9SELL"
    else:
        assert set(query) == {"code", "state"}
    assert query["state"] == params["state"]


@pytest.mark.parametrize("mkt", ["amazon", "flipkart"])
async def test_fake_authorize_rejects_wrong_redirect_uri(fake: httpx.AsyncClient, mkt: str) -> None:
    params = _amazon_params() if mkt == "amazon" else _flipkart_params("v" * 43)
    params["redirect_uri"] = "http://evil.example/cb"
    assert (await fake.get(f"/{mkt}/authorize", params=params)).status_code == 400


async def test_fake_authorize_rejects_plain_pkce(fake: httpx.AsyncClient) -> None:
    params = _flipkart_params("v" * 43, code_challenge_method="plain")
    assert (await fake.get("/flipkart/authorize", params=params)).status_code == 400


async def test_fake_authorize_amazon_rejects_pkce_params(fake: httpx.AsyncClient) -> None:
    params = _amazon_params(code_challenge="c" * 43, code_challenge_method="S256")
    assert (await fake.get("/amazon/authorize", params=params)).status_code == 400
    with_client = _amazon_params(client_id=CFG.amazon.client_id)
    assert (await fake.get("/amazon/authorize", params=with_client)).status_code == 400


async def test_fake_authorize_amazon_rejects_wrong_application_id(
    fake: httpx.AsyncClient,
) -> None:
    params = _amazon_params(application_id="amzn1.sp.solution.OTHER")
    assert (await fake.get("/amazon/authorize", params=params)).status_code == 400


async def test_fake_authorize_missing_param_400(fake: httpx.AsyncClient) -> None:
    params = _flipkart_params("v" * 43)
    del params["state"]
    assert (await fake.get("/flipkart/authorize", params=params)).status_code == 400


async def test_fake_token_checks_verifier(fake: httpx.AsyncClient) -> None:
    verifier = secrets.token_urlsafe(48)
    query = _query(await fake.get("/flipkart/authorize", params=_flipkart_params(verifier)))
    wrong = await _token(
        fake,
        "flipkart",
        grant_type="authorization_code",
        code=query["code"],
        redirect_uri=CFG.flipkart.redirect_uri,
        code_verifier="x" * 64,
    )
    assert wrong.status_code == 400 and wrong.json() == {"error": "invalid_grant"}
    query = _query(await fake.get("/flipkart/authorize", params=_flipkart_params(verifier)))
    ok = await _token(
        fake,
        "flipkart",
        grant_type="authorization_code",
        code=query["code"],
        redirect_uri=CFG.flipkart.redirect_uri,
        code_verifier=verifier,
    )
    body: dict[str, Any] = ok.json()
    assert ok.status_code == 200
    assert body["access_token"].startswith(f"{CANARY}-")
    assert body["refresh_token"].startswith(f"{CANARY}-")
    assert body["seller_id"] == CFG.default_seller
    assert body["expires_in"] == CFG.expires_in


async def _amazon_code(fake: httpx.AsyncClient) -> str:
    query = _query(await fake.get("/amazon/authorize", params=_amazon_params()))
    return query["spapi_oauth_code"]


async def test_fake_token_amazon_rejects_verifier(fake: httpx.AsyncClient) -> None:
    code = await _amazon_code(fake)
    response = await _token(
        fake,
        "amazon",
        grant_type="authorization_code",
        code=code,
        redirect_uri=CFG.amazon.redirect_uri,
        code_verifier="v" * 43,
    )
    assert response.status_code == 400


async def test_fake_token_code_single_use(fake: httpx.AsyncClient) -> None:
    code = await _amazon_code(fake)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CFG.amazon.redirect_uri,
    }
    assert (await _token(fake, "amazon", **form)).status_code == 200
    assert (await _token(fake, "amazon", **form)).json() == {"error": "invalid_grant"}


async def test_fake_token_wrong_client_401(fake: httpx.AsyncClient) -> None:
    code = await _amazon_code(fake)
    response = await fake.post(
        "/amazon/token",
        data={"client_id": CFG.amazon.client_id, "client_secret": "nope", "code": code},
    )
    assert response.status_code == 401 and response.json() == {"error": "invalid_client"}


async def _refresh_token(fake: httpx.AsyncClient) -> str:
    code = await _amazon_code(fake)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CFG.amazon.redirect_uri,
    }
    return str((await _token(fake, "amazon", **form)).json()["refresh_token"])


async def test_fake_token_control_invalid_grant_once(fake: httpx.AsyncClient) -> None:
    refresh = await _refresh_token(fake)
    await fake.post("/__control", json={"next_refresh": "invalid_grant"})
    first = await _token(fake, "amazon", grant_type="refresh_token", refresh_token=refresh)
    assert first.status_code == 400 and first.json() == {"error": "invalid_grant"}
    again = await _token(fake, "amazon", grant_type="refresh_token", refresh_token=refresh)
    assert again.status_code == 200
    assert again.json()["refresh_token"] == refresh  # LWA rotates nothing
    assert again.json()["access_token"].startswith(f"{CANARY}-")


async def test_fake_token_control_server_error(fake: httpx.AsyncClient) -> None:
    refresh = await _refresh_token(fake)
    await fake.post("/__control", json={"next_refresh": "server_error"})
    response = await _token(fake, "amazon", grant_type="refresh_token", refresh_token=refresh)
    assert response.status_code == 500
    await fake.post("/__control", json={"next_exchange": "server_error"})
    code = await _amazon_code(fake)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CFG.amazon.redirect_uri,
    }
    assert (await _token(fake, "amazon", **form)).status_code == 500


async def test_fake_authorize_deny(fake: httpx.AsyncClient) -> None:
    query = _query(await fake.get("/amazon/authorize", params=_amazon_params(deny="1")))
    assert query == {"error": "access_denied", "state": "st-a"}


async def test_fake_healthz(fake: httpx.AsyncClient) -> None:
    assert (await fake.get("/healthz")).text == "ok"


def test_fake_config_from_env_and_dotenv(tmp_path: Any) -> None:
    from tests.support.fake_marketplace import config_from_env

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AMAZON_OAUTH_REDIRECT_URI=http://localhost:8130/api/v1/marketplaces/amazon/callback\n"
        "AMAZON_APP_ID=\nFLIPKART_CLIENT_ID=fk-from-file\n",
        encoding="utf-8",
    )
    cfg = config_from_env({"FLIPKART_CLIENT_ID": "fk-from-env", "AMAZON_APP_ID": ""}, dotenv)
    assert cfg.amazon.redirect_uri.startswith("http://localhost:8130/")
    assert cfg.flipkart.client_id == "fk-from-env"
    assert cfg.amazon_application_id == CFG.amazon_application_id
    assert cfg.amazon.client_secret == CFG.amazon.client_secret
