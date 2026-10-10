"""S22 T2 (AC13; D8; C10): the Flipkart API base and OAuth URLs, pinned off local."""

from __future__ import annotations

import pytest

API = "https://api.flipkart.net/sellers"
TOKEN = "https://api.flipkart.net/oauth-service/oauth/token"
AUTHORIZE = "https://api.flipkart.net/oauth-service/oauth/authorize"


def _api(value: str, app_env: str = "staging") -> list[str]:
    from app.adapters.flipkart.endpoint import flipkart_endpoint_errors

    return flipkart_endpoint_errors(value, app_env=app_env)


def _oauth(
    token: str = TOKEN, authorize: str | None = AUTHORIZE, app_env: str = "staging"
) -> list[str]:
    from app.adapters.flipkart.endpoint import flipkart_oauth_endpoint_errors

    return flipkart_oauth_endpoint_errors(token, authorize, app_env=app_env)


@pytest.mark.parametrize("app_env", ["staging", "production", "local"])
def test_flipkart_api_base_pinned_ok(app_env: str) -> None:
    assert _api(API, app_env) == []
    assert _api(API + "/", app_env) == []


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/sellers",
        "http://api.flipkart.net/sellers",
        "https://api.flipkart.net.evil.example/sellers",
        "https://api.flipkart.net/other",
        "https://api.flipkart.net",
    ],
)
def test_flipkart_api_base_other_host_refused(app_env: str, bad: str) -> None:
    assert _api(bad, app_env) != []


@pytest.mark.parametrize(
    "bad",
    [
        "https://api.flipkart.net:444/sellers",
        "https://u:p@api.flipkart.net/sellers",
        "https://api.flipkart.net/sellers?a=b",
        "https://api.flipkart.net/sellers#x",
        "not a url",
        "https://[::1/sellers",
    ],
)
def test_flipkart_api_base_path_port_userinfo_query_refused(bad: str) -> None:
    assert _api(bad) != []


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:4020/flipkart/sellers",
        "https://localhost:4999/flipkart/sellers",
        "http://[::1]:4020/x",
    ],
)
def test_flipkart_api_base_loopback_ok_on_local(value: str) -> None:
    assert _api(value, "local") == []


@pytest.mark.parametrize(
    "value", ["http://127.0.0.1:4020/flipkart/sellers", "http://u:p@127.0.0.1:4020/x"]
)
def test_flipkart_api_base_loopback_refused_off_local(value: str) -> None:
    assert _api(value, "staging") != []


def test_flipkart_api_base_loopback_userinfo_refused_on_local() -> None:
    assert _api("http://u:p@127.0.0.1:4020/x", "local") != []


def test_flipkart_api_base_non_loopback_http_refused_on_local() -> None:
    assert _api("http://fake.marketplace.test/flipkart/sellers", "local") != []


def test_flipkart_endpoint_messages_name_vars_only() -> None:
    for message in _api("https://leaky-zq9.evil.example/sellers") + _oauth(
        "https://leaky-zq9.evil.example/t", "https://leaky-zq9.evil.example/a"
    ):
        assert "leaky" not in message and "evil" not in message
        assert "FLIPKART_" in message


def test_flipkart_oauth_pinned_ok() -> None:
    assert _oauth() == []
    assert _oauth(authorize=AUTHORIZE + "?x=1") == []
    assert _oauth(authorize=None) == []


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/oauth-service/oauth/token",
        "https://api.flipkart.net/oauth-service/oauth/other",
        "http://api.flipkart.net/oauth-service/oauth/token",
        "https://api.flipkart.net:444/oauth-service/oauth/token",
    ],
)
def test_flipkart_token_url_other_host_refused(app_env: str, bad: str) -> None:
    (message,) = _oauth(token=bad, app_env=app_env)
    assert "FLIPKART_TOKEN_URL" in message


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/oauth-service/oauth/authorize",
        "https://api.flipkart.net.evil.example/oauth-service/oauth/authorize",
        "http://api.flipkart.net/oauth-service/oauth/authorize",
        "https://api.flipkart.net:444/a",
        "https://u:p@api.flipkart.net/a",
        "https://api.flipkart.net/a#frag",
    ],
)
def test_flipkart_authorize_url_other_host_refused(bad: str) -> None:
    (message,) = _oauth(authorize=bad)
    assert "FLIPKART_AUTHORIZE_URL" in message


def test_flipkart_oauth_loopback_ok_on_local_only() -> None:
    local_token = "http://127.0.0.1:4020/flipkart/token"
    local_authorize = "http://127.0.0.1:4020/flipkart/authorize"
    assert _oauth(local_token, local_authorize, "local") == []
    assert len(_oauth(local_token, local_authorize, "staging")) == 2
    assert _oauth("http://fake.marketplace.test/flipkart/token", None, "local") != []
