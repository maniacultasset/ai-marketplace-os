"""Recovery codes in `app.core.recovery_codes`: DPP-1 AC2."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid

import pytest
from app.core import recovery_codes as rc
from hypothesis import given, settings
from hypothesis import strategies as st

DISPLAY = re.compile(r"^[a-z2-7]{4}(-[a-z2-7]{1,4}){6}$")


def test_ten_distinct_codes_each_128_bits() -> None:
    codes = rc.new_codes()
    assert len(codes) == rc.COUNT == 10
    assert len(set(codes)) == 10
    for code in codes:
        assert DISPLAY.match(code), code
        normalised = rc.normalise(code)
        assert normalised is not None and len(normalised) == 26
        # 26 base32 chars carry 130 bits, of which the first 128 are the random bytes.
        assert rc.CODE_BYTES * 8 == 128


def test_codes_use_secrets_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    counter = iter(range(1000))

    def fake_token_bytes(n: int) -> bytes:
        calls.append(n)
        return bytes([next(counter)]) * n

    monkeypatch.setattr(secrets, "token_bytes", fake_token_bytes)
    codes = rc.new_codes()
    assert calls == [16] * 10
    assert len(set(codes)) == 10


def test_duplicate_draw_is_redrawn(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter([b"\x00" * 16, b"\x00" * 16] + [bytes([i]) * 16 for i in range(1, 20)])
    monkeypatch.setattr(secrets, "token_bytes", lambda n: next(draws))
    codes = rc.new_codes()
    assert len(set(codes)) == 10


@pytest.mark.parametrize(
    "variant",
    [
        lambda c: c.upper(),
        lambda c: c.replace("-", ""),
        lambda c: c.replace("-", " "),
        lambda c: "  " + c.replace("-", " - ") + " ",
    ],
)
def test_normalise_tolerates_case_spaces_dashes(variant) -> None:  # type: ignore[no-untyped-def]
    code = rc.new_codes()[0]
    assert rc.normalise(variant(code)) == code.replace("-", "")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "----",
        "a" * 25,
        "a" * 27,
        "0" + "a" * 25,
        "1" + "a" * 25,
        "8" + "a" * 25,
        "9" + "a" * 25,
        "\u0430" + "a" * 25,  # Cyrillic a
        "\uff41" + "a" * 25,  # full-width a
        "a\t" + "a" * 25,
        "a" * 10_000,
    ],
)
def test_normalise_refuses_wrong_length_and_alphabet(bad: str) -> None:
    assert rc.normalise(bad) is None


def test_normalise_non_str_is_none() -> None:
    assert rc.normalise(None) is None  # type: ignore[arg-type]


def test_hash_binds_user_id() -> None:
    code = rc.normalise(rc.new_codes()[0])
    assert code is not None
    a, b = uuid.uuid4(), uuid.uuid4()
    assert rc.code_hash(a, code) != rc.code_hash(b, code)
    assert rc.code_hash(a, code) == hashlib.sha256(a.bytes + code.encode()).digest()
    assert len(rc.code_hash(a, code)) == 32


@settings(max_examples=100)
@given(raw=st.binary(min_size=16, max_size=16))
def test_property_display_then_normalise_round_trips(raw: bytes) -> None:
    shown = rc.display(raw)
    assert DISPLAY.match(shown)
    normalised = rc.normalise(shown)
    assert normalised is not None
    assert rc.normalise(shown.upper().replace("-", " ")) == normalised
