"""The DPP 1.4.1 password rules in `app.core.password_policy`: DPP-1 AC3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core import password_policy as pp
from app.core import passwords
from hypothesis import given, settings
from hypothesis import strategies as st

GOOD = "Tiger-Lamp-0429"
EMAIL = "zx.kumar@example.in"


def rules(plain: str, *, email: str = EMAIL, name: str | None = "Asha Rao") -> set[str]:
    return {b.rule for b in pp.check(plain, email=email, display_name=name)}


@pytest.mark.parametrize(
    ("plain", "email", "name", "expected"),
    [
        (GOOD, EMAIL, "Asha Rao", set()),
        # each class missing
        ("tiger-lamp-0429", EMAIL, None, {"upper"}),
        ("TIGER-LAMP-0429", EMAIL, None, {"lower"}),
        ("Tiger-Lamp-wxyz", EMAIL, None, {"digit"}),
        ("TigerLamp0429x", EMAIL, None, {"symbol"}),
        # length: 11, 12, 1024, 1025 code points
        ("Tig-Lamp-04", EMAIL, None, {"length"}),
        ("Tig-Lamp-042", EMAIL, None, set()),
        ("Tq-0" + "x" * 1020, EMAIL, None, set()),
        ("Tq-0" + "x" * 1021, EMAIL, None, {"length"}),
        # email local part (3+ chars) and its letter runs
        ("Kumar-Lamp-0429", EMAIL, None, {"personal"}),
        ("Zxkumar-Lamp-0429", "zxkumar@example.in", None, {"personal"}),
        # display_name tokens: "Asha Rao" gives asha and rao (3 letters counts)
        ("Asha-Lamp-0429!", EMAIL, "Asha Rao", {"personal"}),
        ("Tiger-Rao-04291", EMAIL, "Asha Rao", {"personal"}),
        # case variants
        ("ASHA-lamp-0429", EMAIL, "Asha Rao", {"personal"}),
        # NFKC look-alike: full-width letters fold to the name
        ("\uff21\uff53\uff48\uff41-Lamp-0429", EMAIL, "Asha Rao", {"personal"}),
        # Devanagari name inside the password
        ("आशा-Lamp-0429x", EMAIL, "आशा राव", {"personal"}),
        # a Devanagari-only password has no cased letter
        ("आशाराव-१२३४-कमल", EMAIL, None, {"upper", "lower"}),
        # a local part under 3 characters is ignored
        ("Zx-Lamp-0429-q", "zx@example.in", None, set()),
        # display_name None or blank
        (GOOD, EMAIL, None, set()),
        (GOOD, EMAIL, "   ", set()),
        # name runs under 3 letters are ignored
        ("Tiger-Lamp-0429", EMAIL, "Al Bo", set()),
        # only spaces as the symbol
        ("Tiger Lamp 0429", EMAIL, None, set()),
        # an emoji as the symbol
        ("TigerLamp0429🙂", EMAIL, None, set()),
        # everything wrong at once
        ("", EMAIL, None, {"length", "upper", "lower", "digit", "symbol"}),
    ],
)
def test_policy_table(plain: str, email: str, name: str | None, expected: set[str]) -> None:
    assert rules(plain, email=email, name=name) == expected


def test_breach_order_is_stable() -> None:
    breaches = pp.check("", email=EMAIL, display_name=None)
    assert [b.rule for b in breaches] == ["length", "upper", "lower", "digit", "symbol"]


def test_messages_are_fixed_strings() -> None:
    for breach in pp.check("", email=EMAIL, display_name=None):
        assert breach.message == pp.MESSAGES[breach.rule]
    assert set(pp.MESSAGES) == {"length", "upper", "lower", "digit", "symbol", "personal"}


@settings(max_examples=200)
@given(
    # Outside ASCII, so a random value cannot equal a word of the fixed English messages.
    plain=st.text(st.characters(min_codepoint=0x100), min_size=3, max_size=40),
    name=st.text(st.characters(min_codepoint=0x100), min_size=3, max_size=20),
)
def test_messages_never_contain_password_or_name(plain: str, name: str) -> None:
    for breach in pp.check(plain, email=f"{name}@example.in", display_name=name):
        if plain.strip() and len(plain) >= 3:
            assert plain not in breach.message
        if name.strip() and len(name) >= 3:
            assert name not in breach.message


NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_too_young_boundary_exactly_24h() -> None:
    assert pp.too_young(NOW - timedelta(hours=23, minutes=59, seconds=59), NOW) is True
    assert pp.too_young(NOW - timedelta(days=1), NOW) is False
    assert pp.too_young(NOW - timedelta(days=2), NOW) is False


def test_expired_boundary_exactly_365d() -> None:
    assert pp.expired(NOW - timedelta(days=364, hours=23), NOW) is False
    assert pp.expired(NOW - timedelta(days=365), NOW) is True
    assert pp.expired(NOW - timedelta(days=400), NOW) is True


def test_constants() -> None:
    assert (pp.PASSWORD_MIN, pp.PASSWORD_MAX, pp.HISTORY) == (12, 1024, 10)
    assert (timedelta(days=1), timedelta(days=365)) == (pp.MIN_AGE, pp.MAX_AGE)


def test_read_password_limits_unchanged() -> None:
    assert passwords.PASSWORD_MIN is pp.PASSWORD_MIN
    assert passwords.PASSWORD_MAX is pp.PASSWORD_MAX


def test_policy_error_carries_breaches_not_input() -> None:
    breaches = pp.check("short", email=EMAIL, display_name=None)
    err = pp.PolicyError(breaches)
    assert "short" not in str(err)
    assert err.breaches == breaches
    assert str(err) == "; ".join(b.message for b in breaches)
