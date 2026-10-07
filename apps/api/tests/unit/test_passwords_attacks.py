"""S9 T1 attacks on `app.core.passwords`: foreign, truncated and tampered hashes never raise."""

from __future__ import annotations

import pytest
from app.core.passwords import hash_password, verify_password

GOOD = hash_password("devpassword123")


@pytest.mark.parametrize(
    "hashed",
    [
        "$2b$12$abcdefghijklmnopqrstuuJ0jzVg3Q5bZ9mY1kFvJwz3A2xQ0hQeK",  # bcrypt
        "$argon2id$",
        "$argon2id$v=19$m=65536,t=3,p=4$",
        "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA",
        pytest.param(GOOD[:-4], id="good-truncated"),  # a fixed id: GOOD is salted per process
        pytest.param(GOOD + "AAAA", id="good-extended"),
        pytest.param(GOOD.replace("m=65536", "m=1"), id="good-m1"),
        "plaintext",
        "\x00",
    ],
)
def test_verify_foreign_or_tampered_hash_is_false(hashed: str) -> None:
    assert verify_password("devpassword123", hashed) is False


def test_hash_is_argon2id_and_salted() -> None:
    assert GOOD.startswith("$argon2id$")
    assert hash_password("devpassword123") != GOOD


@pytest.mark.parametrize("plain", ["pässwörd", "密码🔐", "a" * 4096, " leading space"])
def test_unicode_and_long_round_trip(plain: str) -> None:
    hashed = hash_password(plain)
    assert verify_password(plain, hashed) is True
    assert verify_password(plain + "x", hashed) is False


def test_empty_plain_against_real_hash_is_false() -> None:
    assert verify_password("", GOOD) is False
