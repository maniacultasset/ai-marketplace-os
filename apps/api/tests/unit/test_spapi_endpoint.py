"""S20 T16 (AC16; D10; C17): the SP-API endpoint pinned to AMAZON_USE_SANDBOX (pure)."""

from __future__ import annotations

import pytest

SANDBOX = "https://sandbox.sellingpartnerapi-eu.amazon.com"
PROD = "https://sellingpartnerapi-eu.amazon.com"


def _errors(endpoint: str, *, sandbox: bool, env: str) -> list[str]:
    from app.adapters.amazon.endpoint import spapi_endpoint_errors

    return spapi_endpoint_errors(endpoint, use_sandbox=sandbox, app_env=env)


def test_spapi_hosts_constants() -> None:
    from app.adapters.amazon.endpoint import PROD_HOST, SANDBOX_HOST

    assert (SANDBOX_HOST, PROD_HOST) == (SANDBOX, PROD)


@pytest.mark.parametrize("env", ["local", "staging", "production"])
def test_spapi_sandbox_host_with_flag_true_ok(env: str) -> None:
    assert _errors(SANDBOX, sandbox=True, env=env) == []
    assert _errors(SANDBOX + "/", sandbox=True, env=env) == []


@pytest.mark.parametrize("env", ["local", "staging", "production"])
def test_spapi_prod_host_with_flag_false_ok(env: str) -> None:
    assert _errors(PROD, sandbox=False, env=env) == []


@pytest.mark.parametrize("env", ["local", "staging", "production"])
def test_spapi_prod_host_with_sandbox_true_refused(env: str) -> None:
    """The `.env.example` pair: the production host with the sandbox flag on."""
    assert _errors(PROD, sandbox=True, env=env) == [
        "AMAZON_SPAPI_ENDPOINT must be the SP-API sandbox host while AMAZON_USE_SANDBOX=true"
    ]


def test_spapi_sandbox_host_with_flag_false_refused() -> None:
    assert _errors(SANDBOX, sandbox=False, env="production") == [
        "AMAZON_SPAPI_ENDPOINT must be the SP-API production host while AMAZON_USE_SANDBOX=false"
    ]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://sellingpartnerapi-fe.amazon.com",
        "https://sellingpartnerapi-eu.amazon.com.evil.test",
        "https://evil.test",
        "http://sandbox.sellingpartnerapi-eu.amazon.com",
    ],
)
@pytest.mark.parametrize("env", ["staging", "production"])
def test_spapi_unknown_or_plain_host_refused(endpoint: str, env: str) -> None:
    assert _errors(endpoint, sandbox=True, env=env) != []


@pytest.mark.parametrize(
    "endpoint",
    [
        SANDBOX + ":8443",
        SANDBOX + "/listings",
        "https://user:pw@sandbox.sellingpartnerapi-eu.amazon.com",
        SANDBOX + "?x=1",
        SANDBOX + "#f",
        "https://[::1",
    ],
    ids=["port", "path", "userinfo", "query", "fragment", "unparseable"],
)
def test_spapi_path_port_userinfo_query_refused(endpoint: str) -> None:
    assert _errors(endpoint, sandbox=True, env="staging") != []


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1:4020", "http://localhost:4020/", "https://[::1]:4020"]
)
def test_spapi_loopback_ok_on_local(endpoint: str) -> None:
    assert _errors(endpoint, sandbox=True, env="local") == []


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:4020", "https://localhost"])
@pytest.mark.parametrize("env", ["staging", "production"])
def test_spapi_loopback_refused_off_local(endpoint: str, env: str) -> None:
    assert _errors(endpoint, sandbox=True, env=env) != []


def test_spapi_loopback_with_path_refused_on_local() -> None:
    assert _errors("http://127.0.0.1:4020/x?y=1", sandbox=True, env="local") != []


@pytest.mark.parametrize("value", ["https://sellingpartnerapi-eu.amazon.com.evil.test", "https://zq9usr:zq9pw@x.test", "http://127.0.0.1:4020/secret"])  # fmt: skip
def test_spapi_message_names_env_vars_only(value: str) -> None:
    for message in _errors(value, sandbox=True, env="staging"):
        assert "AMAZON_SPAPI_ENDPOINT" in message
        for leak in ("evil", "zq9usr", "zq9pw", "127.0.0.1", "secret", "x.test"):
            assert leak not in message
