"""S10 C7 (DW7, AC7; CODE-PLAN Q6; KNOWN-ISSUES.md:37): the limiter's client IP comes from the
entry the trusted proxy appends, never from the left-most (caller-written) X-Forwarded-For entry."""

from __future__ import annotations

from typing import get_args

import pytest
from app.api.rate_limit import HOPS, client_ip, login_bucket

PEER = "10.0.0.9"


def test_client_ip_local_ignores_xff() -> None:
    assert client_ip(["1.1.1.1, 2.2.2.2"], PEER, HOPS["local"]) == PEER
    assert client_ip(["1.1.1.1"], PEER, 0) == PEER


def test_client_ip_production_second_from_right() -> None:
    # <forged>,<client>,<lb>: the load balancer appended the last two
    assert client_ip(["6.6.6.6, 203.0.113.5, 130.211.0.1"], PEER, HOPS["production"]) == (
        "203.0.113.5"
    )


def test_client_ip_lb_only_header() -> None:
    assert client_ip(["203.0.113.5, 130.211.0.1"], PEER, HOPS["production"]) == "203.0.113.5"


def test_client_ip_staging_hops() -> None:
    assert HOPS["staging"] == 2
    assert client_ip(["9.9.9.9,198.51.100.4,35.191.0.7"], PEER, HOPS["staging"]) == "198.51.100.4"


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
    assert dict(HOPS) == {"local": 0, "staging": 2, "production": 2}


@pytest.mark.parametrize("email", ["Owner@Acme.test", " owner@acme.test ", "OWNER@ACME.TEST"])
def test_login_bucket_folds_email(email: str) -> None:
    assert login_bucket("1.2.3.4", email) == login_bucket("1.2.3.4", "owner@acme.test")
    assert login_bucket("1.2.3.4", email).startswith("login:")
    assert "acme" not in login_bucket("1.2.3.4", email)
    assert login_bucket("1.2.3.5", email) != login_bucket("1.2.3.4", email)
