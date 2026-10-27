"""TOTP (RFC 6238) and HOTP (RFC 4226) in `app.core.totp`: DPP-1 AC1."""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from app.core import totp
from hypothesis import given, settings
from hypothesis import strategies as st

# RFC 6238 Appendix B: the SHA1 seed is the 20 ASCII bytes "12345678901234567890".
SEED = b"12345678901234567890"


@pytest.mark.parametrize(
    ("unix", "eight_digits"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_appendix_b_sha1_vectors(unix: int, eight_digits: str) -> None:
    code = totp.hotp(SEED, totp.step_at(unix))
    assert code == eight_digits[-6:]
    assert totp.match_step(SEED, eight_digits[-6:], unix) == totp.step_at(unix)


RFC4226_D = [
    "755224",
    "287082",
    "359152",
    "969429",
    "338314",
    "254676",
    "287922",
    "162583",
    "399871",
    "520489",
]


@pytest.mark.parametrize(("counter", "expected"), list(enumerate(RFC4226_D)))
def test_rfc4226_appendix_d_hotp_vectors(counter: int, expected: str) -> None:
    assert totp.hotp(SEED, counter) == expected


NOW = 1_700_000_015.0


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_accepts_t_minus_1_t_t_plus_1(delta: int) -> None:
    step = totp.step_at(NOW) + delta
    assert totp.match_step(SEED, totp.hotp(SEED, step), NOW) == step


@pytest.mark.parametrize("delta", [-2, 2, -3, 3])
def test_refuses_t_plus_minus_2(delta: int) -> None:
    step = totp.step_at(NOW) + delta
    assert totp.match_step(SEED, totp.hotp(SEED, step), NOW) is None


def test_match_returns_the_matched_step() -> None:
    step = totp.step_at(NOW)
    assert totp.match_step(SEED, totp.hotp(SEED, step - 1), NOW) == step - 1
    assert totp.match_step(SEED, totp.hotp(SEED, step + 1), NOW) == step + 1


def test_step_boundary_29_999_vs_30_000() -> None:
    assert totp.step_at(29.999) == 0
    assert totp.step_at(30.0) == 1
    assert totp.step_at(59.999) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "12345",
        "1234567",
        " 123456",
        "123456 ",
        "12345a",
        "١٢٣٤٥٦",
        "\uff11\uff12\uff13\uff14\uff15\uff16",
        "NaN",
        "Infinity",
        "1" * 10_240,
    ],
)
def test_bad_code_shapes_return_none(bad: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = totp.hotp

    def spy(secret: bytes, counter: int) -> str:
        calls.append(counter)
        return real(secret, counter)

    monkeypatch.setattr(totp, "hotp", spy)
    assert totp.match_step(SEED, bad, NOW) is None
    assert calls == []


def test_non_str_code_returns_none() -> None:
    assert totp.match_step(SEED, 123456, NOW) is None  # type: ignore[arg-type]


def test_secret_is_20_bytes_and_b32_unpadded() -> None:
    secret = totp.new_secret()
    assert isinstance(secret, bytes) and len(secret) == totp.SECRET_BYTES == 20
    encoded = totp.b32(secret)
    assert "=" not in encoded and encoded == encoded.upper()
    assert len(encoded) == 32
    assert base64.b32decode(encoded) == secret
    assert totp.new_secret() != secret


def test_b32_of_odd_length_has_no_padding() -> None:
    encoded = totp.b32(b"abc")
    assert "=" not in encoded
    with pytest.raises(binascii.Error):
        base64.b32decode(encoded)  # unpadded on purpose: authenticator apps want it bare


def test_otpauth_uri_is_parseable_and_quotes_label() -> None:
    secret = SEED
    account = "asha+ops@kalā.in"
    uri = totp.otpauth_uri(secret, account=account)
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    issuer, _, label_account = unquote(parts.path.lstrip("/")).partition(":")
    assert issuer == totp.MFA_ISSUER == "Kuinn"
    assert label_account == account
    assert "+" not in parts.path and "@" not in parts.path
    query = parse_qs(parts.query, strict_parsing=True)
    assert query == {
        "secret": [totp.b32(secret)],
        "issuer": ["Kuinn"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


def test_constants() -> None:
    assert (totp.STEP_SECONDS, totp.DIGITS, totp.WINDOW, totp.SECRET_BYTES) == (30, 6, 1, 20)


@settings(max_examples=200)
@given(
    secret=st.binary(min_size=20, max_size=20),
    unix=st.integers(min_value=60, max_value=2**34),
    delta=st.integers(min_value=-1, max_value=1),
)
def test_property_hotp_of_step_matches_within_window(secret: bytes, unix: int, delta: int) -> None:
    step = totp.step_at(unix) + delta
    matched = totp.match_step(secret, totp.hotp(secret, step), unix)
    # Another step in the window can collide on 6 digits (1 in 10^6); the match is still in window.
    assert matched is not None
    assert abs(matched - totp.step_at(unix)) <= totp.WINDOW
