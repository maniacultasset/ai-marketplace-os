"""Trusted-browser tokens and the coarse browser label (ADR-0023)."""

from __future__ import annotations

import re

import pytest
from app.core import device_tokens, user_agent


def test_new_token_is_256_bit_base64url() -> None:
    tokens = {device_tokens.new_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)


def test_digest_is_domain_separated_sha256() -> None:
    import hashlib

    token = device_tokens.new_token()
    digest = device_tokens.digest(token)
    assert len(digest) == 32
    assert digest != hashlib.sha256(token.encode()).digest()
    assert digest == hashlib.sha256(b"mkos-trusted-device|" + token.encode()).digest()


def test_matches_is_constant_time_compare() -> None:
    token = device_tokens.new_token()
    digest = device_tokens.digest(token)
    assert device_tokens.matches(digest, device_tokens.digest(token))
    assert not device_tokens.matches(digest, device_tokens.digest(device_tokens.new_token()))
    assert not device_tokens.matches(digest, None)


@pytest.mark.parametrize(
    "value", ["", "a" * 42, "a" * 44, "a" * 42 + "=", "a" * 42 + "é", "a" * 42 + "/"]
)
def test_well_formed_refuses_anything_else(value: str) -> None:
    assert device_tokens.well_formed(value) is False


def test_well_formed_accepts_a_token() -> None:
    assert device_tokens.well_formed(device_tokens.new_token())


UAS = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36",
        "Chrome on macOS",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Edge on Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Firefox on Windows",
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
        "Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        "Safari on iOS",
    ),
    (
        "Mozilla/5.0 (iPad; CPU OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.6 Mobile/15E148 Safari/604.1",
        "Safari on iPadOS",
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
        "Samsung Internet on Android",
    ),
    (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Mobile Safari/537.36",
        "Chrome on Android",
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 "
        "Safari/537.36 OPR/114.0.0.0",
        "Opera on Linux",
    ),
    (
        "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36",
        "Chrome on ChromeOS",
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
        "Gecko) CriOS/129.0.6668.69 Mobile/15E148 Safari/604.1",
        "Chrome on iOS",
    ),
    ("curl/8.7.1", "Browser"),
]


@pytest.mark.parametrize(("ua", "label"), UAS)
def test_summary(ua: str, label: str) -> None:
    assert user_agent.summary(ua) == label


@pytest.mark.parametrize("ua", [None, "", " ", "\x00" * 10, "x" * 4096, "Chrome/" * 700])
def test_summary_of_empty_or_garbage(ua: str | None) -> None:
    label = user_agent.summary(ua)
    assert 1 <= len(label) <= 64
    assert label.isprintable()
