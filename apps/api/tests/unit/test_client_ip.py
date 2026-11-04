"""S10 C7 (DW7, AC7; CODE-PLAN Q6; KNOWN-ISSUES.md:37): the limiter's client IP comes from the
entry the trusted proxy appends, never from the left-most (caller-written) X-Forwarded-For entry."""

from __future__ import annotations

from typing import get_args

import pytest
from app.api.rate_limit import HOPS, client_ip, login_bucket, login_email_bucket

PEER = "10.0.0.9"


def test_client_ip_local_ignores_xff() -> None:
    assert client_ip(["1.1.1.1, 2.2.2.2"], PEER, HOPS["local"]) == PEER
    assert client_ip(["1.1.1.1"], PEER, 0) == PEER


def test_client_ip_production_right_most() -> None:
    # run.app: Google's front end appends only <client>; anything left of it is caller-written
    assert client_ip(["203.0.113.5"], PEER, HOPS["production"]) == "203.0.113.5"
    assert client_ip(["6.6.6.6, 203.0.113.5"], PEER, HOPS["production"]) == "203.0.113.5"


def test_client_ip_forged_entries_never_pick_the_bucket() -> None:
    # the live bypass: a rotating forged left-most entry must not change the bucket
    for forged in ("198.51.100.7", "198.51.100.8", "1.1.1.1, 2.2.2.2"):
        assert client_ip([f"{forged}, 203.0.113.5"], PEER, HOPS["production"]) == "203.0.113.5"


def test_client_ip_behind_a_load_balancer() -> None:
    # <forged>,<client>,<lb>: with an ALB in front, hops becomes 2
    assert client_ip(["6.6.6.6, 203.0.113.5, 130.211.0.1"], PEER, 2) == "203.0.113.5"


def test_client_ip_staging_hops() -> None:
    assert HOPS["staging"] == 1
    assert client_ip(["9.9.9.9,198.51.100.4"], PEER, HOPS["staging"]) == "198.51.100.4"


def test_client_ip_fewer_entries_than_hops() -> None:
    assert client_ip(["203.0.113.5"], PEER, 2) == "203.0.113.5"
    assert client_ip(["a, b"], PEER, 5) == "a"


def test_client_ip_no_header_peer() -> None:
    assert client_ip([], PEER, 2) == PEER
    assert client_ip([], None, 2) == "unknown"
    assert client_ip([""], PEER, 2) == PEER


def test_client_ip_blank_entries() -> None:
    # blank entries never shift the position the proxy wrote
    assert client_ip([" , 203.0.113.5 , , 130.211.0.1 ,"], PEER, 2) == "203.0.113.5"
    assert client_ip([",,,"], PEER, 2) == PEER


def test_client_ip_repeated_headers_joined() -> None:
    assert client_ip(["6.6.6.6", "203.0.113.5, 130.211.0.1"], PEER, 2) == "203.0.113.5"


def test_client_ip_capped() -> None:
    assert len(client_ip(["x" * 500 + ", lb"], PEER, 2)) == 64


def test_hops_table_literal() -> None:
    from app.core.config import Settings

    app_envs = get_args(Settings.model_fields["app_env"].annotation)
    assert set(HOPS) == set(app_envs) == {"local", "staging", "production"}
    assert dict(HOPS) == {"local": 0, "staging": 1, "production": 1}


@pytest.mark.parametrize("email", ["Owner@Acme.test", " owner@acme.test ", "OWNER@ACME.TEST"])
def test_login_bucket_folds_email(email: str) -> None:
    assert login_bucket("1.2.3.4", email) == login_bucket("1.2.3.4", "owner@acme.test")
    assert login_bucket("1.2.3.4", email).startswith("login:")
    assert "acme" not in login_bucket("1.2.3.4", email)
    assert login_bucket("1.2.3.5", email) != login_bucket("1.2.3.4", email)


def test_login_email_bucket_ignores_ip_and_case() -> None:
    assert login_email_bucket(" Owner@Acme.test ") == login_email_bucket("owner@acme.test")
    assert login_email_bucket("owner@acme.test").startswith("login-email:")
    assert "acme" not in login_email_bucket("owner@acme.test")
    assert login_email_bucket("owner@acme.test") != login_bucket("1.2.3.4", "owner@acme.test")


# ---- ADR-0025 (EDGE-CF T2b, AC12): the Worker's x-kuinn-client-ip ----

EDGE_SECRET = "Zq9-edge-origin-secret-current-32"


def _scope(ok: object, *values: bytes, xff: bytes = b"6.6.6.6, 198.51.100.1") -> dict[str, object]:
    from app.api.origin_auth import SCOPE_KEY

    headers = [(b"x-kuinn-client-ip", value) for value in values]
    headers.append((b"x-forwarded-for", xff))
    scope: dict[str, object] = {"type": "http", "headers": headers, "client": (PEER, 1)}
    if ok is not None:
        scope[SCOPE_KEY] = ok
    return scope


def _request_ip(scope: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> str:
    from app.api import rate_limit
    from starlette.requests import Request

    class _Settings:
        app_env = "production"

    monkeypatch.setattr(rate_limit, "get_settings", lambda: _Settings())
    return rate_limit.request_ip(Request(scope))


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (b"203.0.113.5", "203.0.113.5"),
        (b" 203.0.113.5 ", "203.0.113.5"),
        (b"2001:DB8::1", "2001:db8::1"),
    ],
)
def test_edge_ip_trusted_with_match(monkeypatch: pytest.MonkeyPatch, raw: bytes, want: str) -> None:
    from app.api.rate_limit import edge_client_ip

    assert edge_client_ip(_scope(True, raw)) == want
    assert _request_ip(_scope(True, raw), monkeypatch) == want


@pytest.mark.parametrize("ok", [None, False, "True", 1])
def test_edge_ip_ignored_without_match(monkeypatch: pytest.MonkeyPatch, ok: object) -> None:
    # the XFF result (right-most entry, HOPS["production"] = 1) is used instead
    assert _request_ip(_scope(ok, b"203.0.113.5"), monkeypatch) == "198.51.100.1"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"unknown",
        b"1.2.3.4:80",
        b"1.2.3.4, 5.6.7.8",
        b"999.1.1.1",
        b"fe80::1%eth0",
        b"1" * 4096,
        "\u0661.\u0662.\u0663.\u0664".encode(),  # Arabic-Indic digits
        b"\xc2\xa01.2.3.4",
    ],
)
def test_edge_ip_ignored_when_not_an_ip(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    assert _request_ip(_scope(True, raw), monkeypatch) == "198.51.100.1"


def test_edge_ip_two_headers_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    scope = _scope(True, b"203.0.113.5", b"203.0.113.6")
    assert _request_ip(scope, monkeypatch) == "198.51.100.1"


def _login_buckets(
    monkeypatch: pytest.MonkeyPatch, mode: str, headers: list[dict[str, str]]
) -> list[str]:
    """Bucket keys two failed logins land in (the limiter is Postgres-backed and unit tests have
    no socket, so `hit` records the key and answers 429 on the login bucket)."""
    from app.core.config import get_settings
    from app.main import create_app
    from app.services import rate_limit
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ORIGIN_AUTH", mode)
    monkeypatch.setenv("EDGE_ORIGIN_SECRET", EDGE_SECRET)
    get_settings.cache_clear()
    seen: list[str] = []

    async def fake_hit(bucket: str, limit: int) -> int:
        seen.append(bucket)
        return limit + 1 if bucket.startswith("login:") else 1

    monkeypatch.setattr(rate_limit, "hit", fake_hit)
    client = TestClient(create_app())
    for extra in headers:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "a@example.com", "password": "not-the-password"},
            headers=extra,
        )
        assert response.status_code == 429, response.text
    return seen


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_edge_ip_separate_buckets(
    ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from app.api.rate_limit import login_bucket

    origin = {"x-kuinn-origin-auth": EDGE_SECRET, "x-forwarded-for": "104.16.0.1"}
    seen = _login_buckets(
        monkeypatch,
        mode,
        [
            {**origin, "x-kuinn-client-ip": "203.0.113.5"},
            {**origin, "x-kuinn-client-ip": "203.0.113.6"},
        ],
    )
    ip_buckets = [b for b in seen if b.startswith("ip:")]
    login_buckets = [b for b in seen if b.startswith("login:")]
    assert ip_buckets == ["ip:203.0.113.5", "ip:203.0.113.6"]
    assert login_buckets == [
        login_bucket("203.0.113.5", "a@example.com"),
        login_bucket("203.0.113.6", "a@example.com"),
    ]


def test_edge_ip_forged_on_run_app(ci_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # no origin secret: the forged header is stripped, both logins share the peer's bucket
    seen = _login_buckets(
        monkeypatch,
        "off",
        [{"x-kuinn-client-ip": "203.0.113.5"}, {"x-kuinn-client-ip": "203.0.113.6"}],
    )
    login_buckets = [b for b in seen if b.startswith("login:")]
    assert len(login_buckets) == 2
    assert login_buckets[0] == login_buckets[1]
