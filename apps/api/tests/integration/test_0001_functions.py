"""0001 functions: the GUC helpers (DATABASE.md §4 "GUC contract"), `gtin_valid` (§5.6
"Identifier") and the `set_updated_at` trigger function (§1 conventions)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

_SAMPLE_UUID = uuid.UUID("6f1c2a4e-0b7d-4c55-9a6e-3f2d1b0c9e87")
_DIGEST = hashlib.sha256(b"s6 token probe").digest()

GUC_HELPERS = [
    ("app_tenant_id", "app.tenant_id", str(_SAMPLE_UUID), _SAMPLE_UUID),
    ("app_user_id", "app.user_id", str(_SAMPLE_UUID), _SAMPLE_UUID),
    ("app_job_id", "app.job_id", str(_SAMPLE_UUID), _SAMPLE_UUID),
    ("app_purge_tenant", "app.purge_tenant", str(_SAMPLE_UUID), _SAMPLE_UUID),
    ("app_login_email", "app.login_email", "a@x.test", "a@x.test"),
    ("app_token_hash", "app.token_hash", _DIGEST.hex(), _DIGEST),
]


def _scalar(conn: Conn, query: str, params: tuple[Any, ...] | None = None) -> Any:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(("helper", "guc", "raw", "expected"), GUC_HELPERS)
def test_guc_helpers(db: Conn, helper: str, guc: str, raw: str, expected: object) -> None:
    call = f"SELECT {helper}()"
    assert _scalar(db, call) is None  # never set in this session
    db.execute("SELECT set_config(%s, '', true)", (guc,))
    assert _scalar(db, call) is None  # a pooled connection resets it to ''
    db.execute("SELECT set_config(%s, %s, true)", (guc, raw))
    value = _scalar(db, call)
    if isinstance(expected, bytes):
        assert bytes(value) == expected
        assert len(bytes(value)) == 32
    else:
        assert value == expected


def _gtin_reference(value: str) -> bool:
    """GS1 check digit over GTIN-8/12/13/14 (ASCII digits only), left-padded to 14. Every other
    length, 9-11 included, is not a GTIN even when its padded check digit happens to fit."""
    if len(value) not in (8, 12, 13, 14) or not all("0" <= c <= "9" for c in value):
        return False
    padded = value.rjust(14, "0")
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(padded[:13]))
    return (10 - total % 10) % 10 == int(padded[13])


GTIN_CASES = [
    ("96385074", True),  # GTIN-8
    ("036000291452", True),  # GTIN-12 (UPC-A)
    ("4006381333931", True),  # GTIN-13 (EAN-13)
    ("04006381333931", True),  # its GTIN-14 spelling
    ("10614141000415", True),  # GTIN-14
    ("4006381333932", False),  # bad check digit
    ("", False),
    ("12345", False),
    ("400638133393100", False),  # 15 digits
    ("4006381333931 ", False),
    ("40063813339\uff131", False),  # a full-width digit
    ("abc", False),
    ("000000017", False),  # 9 digits, valid once padded to 14
    ("0000000017", False),  # 10 digits, likewise
    ("00000000017", False),  # 11 digits, likewise
]


@pytest.mark.parametrize(("value", "expected"), GTIN_CASES)
def test_gtin_valid(db: Conn, value: str, expected: bool) -> None:
    assert _gtin_reference(value) is expected
    assert _scalar(db, "SELECT gtin_valid(%s)", (value,)) is expected


def test_gtin_valid_null_is_false(db: Conn) -> None:
    assert _scalar(db, "SELECT gtin_valid(NULL)") is False


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(digits=st.text(alphabet="0123456789", min_size=1, max_size=16))
def test_gtin_valid_matches_python_reference(db: Conn, digits: str) -> None:
    assert _scalar(db, "SELECT gtin_valid(%s)", (digits,)) is _gtin_reference(digits)


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(value=st.text(max_size=20).filter(lambda s: "\x00" not in s))
def test_gtin_valid_never_raises(db: Conn, value: str) -> None:
    assert _scalar(db, "SELECT gtin_valid(%s)", (value,)) is _gtin_reference(value)


def test_set_updated_at_is_trigger_function(db: Conn) -> None:
    row = db.execute(
        "SELECT p.prorettype = 'trigger'::regtype, p.prolang = "
        "(SELECT oid FROM pg_language WHERE lanname = 'plpgsql') "
        "FROM pg_proc p WHERE p.oid = 'public.set_updated_at()'::regprocedure"
    ).fetchone()
    assert row == (True, True)
