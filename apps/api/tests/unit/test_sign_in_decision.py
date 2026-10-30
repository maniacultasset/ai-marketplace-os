"""The pure sign-in and lock decisions in `app.services.sign_in_policy`: DPP-1 AC5."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from app.services import sign_in_policy as sp

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
GRACE = {
    "unset": None,
    "future": NOW + timedelta(days=3),
    "past": NOW - timedelta(seconds=1),
}
PROVEN = {
    "pwd": frozenset({"pwd"}),
    "pwd+otp": frozenset({"pwd", "otp"}),
    "pwd+rc": frozenset({"pwd", "rc"}),
}


def facts(**overrides: object) -> sp.SignInFacts:
    base = sp.SignInFacts(
        enforcement="all",
        grace_until=None,
        now=NOW,
        is_platform_admin=False,
        is_active=True,
        has_live_membership=True,
        has_active_factor=True,
        first_login=False,
        password_changed_at=NOW - timedelta(days=30),
        password_must_change=False,
        password_compliant=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def expected_step(
    enforcement: str, grace: str, factor: bool, admin: bool, first: bool, proven: str
) -> str:
    """The truth table, written out independently of the module."""
    if enforcement == "off":
        return "signed_in"
    if factor:
        return "signed_in" if proven != "pwd" else "mfa_required"
    grace_running = grace == "future" and not admin and not first
    return "signed_in" if grace_running else "mfa_enrollment_required"


MATRIX = list(
    itertools.product(["off", "all"], GRACE, [True, False], [True, False], [True, False], PROVEN)
)


@pytest.mark.parametrize(("enforcement", "grace", "factor", "admin", "first", "proven"), MATRIX)
def test_matrix(
    enforcement: str, grace: str, factor: bool, admin: bool, first: bool, proven: str
) -> None:
    f = facts(
        enforcement=enforcement,
        grace_until=GRACE[grace],
        has_active_factor=factor,
        is_platform_admin=admin,
        first_login=first,
    )
    assert sp.next_step(f, proven=PROVEN[proven]) == expected_step(
        enforcement, grace, factor, admin, first, proven
    )


@pytest.mark.parametrize("enforcement", ["off", "all"])
@pytest.mark.parametrize("factor", [True, False])
@pytest.mark.parametrize(
    "override", [{"is_active": False}, {"has_live_membership": False}], ids=["inactive", "nomember"]
)
@pytest.mark.parametrize("proven", list(PROVEN))
def test_inactive_or_no_membership_is_none(
    enforcement: str, factor: bool, override: dict[str, bool], proven: str
) -> None:
    f = facts(enforcement=enforcement, has_active_factor=factor, **override)
    assert sp.next_step(f, proven=PROVEN[proven]) is None


OTP = PROVEN["pwd+otp"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"password_changed_at": NOW - timedelta(days=364)}, "signed_in"),
        ({"password_changed_at": NOW - timedelta(days=365)}, "password_change_required"),
        ({"password_must_change": True}, "password_change_required"),
        ({"password_compliant": False}, "password_change_required"),
        ({"password_compliant": None}, "signed_in"),
    ],
)
def test_password_age_cases(overrides: dict[str, object], expected: str) -> None:
    assert sp.next_step(facts(**overrides), proven=OTP) == expected
    # `off` ignores all three
    assert sp.next_step(facts(enforcement="off", **overrides), proven=OTP) == "signed_in"


def test_password_change_in_grace_without_factor() -> None:
    f = facts(has_active_factor=False, grace_until=GRACE["future"], password_compliant=False)
    assert sp.next_step(f, proven=PROVEN["pwd"]) == "password_change_required"


def test_factor_noncompliant_otp_is_password_change_required() -> None:
    step1 = facts(password_compliant=False)
    assert sp.next_step(step1, proven=PROVEN["pwd"]) == "mfa_required"
    assert sp.must_persist_noncompliance(step1) is True
    step2 = facts(password_compliant=None, password_must_change=True)
    assert sp.next_step(step2, proven=OTP) == "password_change_required"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"enforcement": "off", "password_compliant": False}, False),
        ({"password_compliant": False, "password_must_change": True}, False),
        ({"password_compliant": True}, False),
        ({"password_compliant": None}, False),
        ({"password_compliant": False}, True),
    ],
)
def test_must_persist_noncompliance_matrix(overrides: dict[str, object], expected: bool) -> None:
    assert sp.must_persist_noncompliance(facts(**overrides)) is expected


def test_lock_expiry_boundary() -> None:
    assert sp.is_locked(None, NOW) is False
    assert sp.is_locked(NOW, NOW) is False
    assert sp.is_locked(NOW + timedelta(microseconds=1), NOW) is True
    assert sp.is_locked(NOW - timedelta(seconds=1), NOW) is False


@pytest.mark.parametrize(
    ("enforcement", "grace", "admin", "mfa_at", "expected"),
    [
        ("off", None, False, None, True),
        ("off", None, True, None, True),
        ("all", None, False, None, False),
        ("all", GRACE["past"], False, None, False),
        ("all", GRACE["future"], False, None, True),
        ("all", GRACE["future"], True, None, False),  # admins get no grace
        ("all", None, False, NOW - timedelta(days=6, hours=23), True),
        ("all", None, False, NOW - timedelta(days=7), False),
        ("all", None, True, NOW - timedelta(days=1), True),
    ],
)
def test_refresh_allowed_matrix(
    enforcement: str, grace: datetime | None, admin: bool, mfa_at: datetime | None, expected: bool
) -> None:
    assert (
        sp.refresh_allowed(
            enforcement=enforcement,  # type: ignore[arg-type]
            grace_until=grace,
            is_platform_admin=admin,
            mfa_at=mfa_at,
            now=NOW,
        )
        is expected
    )


@pytest.mark.parametrize(("age", "fresh"), [(1799, True), (1800, True), (1801, False)])
def test_step_up_fresh_1799_1800_1801(age: int, fresh: bool) -> None:
    mfa_at = int(NOW.timestamp()) - age
    assert sp.step_up_fresh(mfa_at, ["pwd", "otp"], NOW, otp_only=False) is fresh
    assert sp.step_up_fresh(mfa_at, ["pwd", "otp"], NOW, otp_only=True) is fresh


def test_step_up_otp_only_refuses_rc() -> None:
    mfa_at = int(NOW.timestamp()) - 10
    assert sp.step_up_fresh(mfa_at, ["pwd", "rc"], NOW, otp_only=True) is False
    assert sp.step_up_fresh(mfa_at, ["pwd", "rc"], NOW, otp_only=False) is True


@pytest.mark.parametrize(
    ("mfa_at", "amr"), [(None, ["pwd", "otp"]), (1, None), (1, []), (1, ["pwd"])]
)
def test_step_up_needs_mfa_at_and_a_factor(mfa_at: int | None, amr: list[str] | None) -> None:
    now = datetime.fromtimestamp(2, tz=UTC)
    assert sp.step_up_fresh(mfa_at, amr, now, otp_only=False) is False


def test_in_grace() -> None:
    assert sp.in_grace(facts(has_active_factor=False, grace_until=GRACE["future"])) is True
    assert sp.in_grace(facts(has_active_factor=True, grace_until=GRACE["future"])) is False
    assert sp.in_grace(facts(has_active_factor=False, grace_until=GRACE["past"])) is False
    assert (
        sp.in_grace(facts(enforcement="off", has_active_factor=False, grace_until=GRACE["future"]))
        is False
    )


def test_constants_match_dpp() -> None:
    assert (
        sp.LOCK_THRESHOLD,
        sp.LOCK_SECONDS,
        sp.CHALLENGE_TTL_SECONDS,
        sp.ENROLL_CHALLENGE_TTL_SECONDS,
        sp.CHALLENGE_MAX_ATTEMPTS,
        sp.STEP_UP_MAX_AGE_SECONDS,
        sp.REAUTH_MAX_AGE_DAYS,
    ) == (10, 900, 300, 900, 5, 1800, 7)


def test_module_imports_no_db_or_web() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(sp.__file__).read_text(encoding="utf-8"))
    modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    banned = ("sqlalchemy", "fastapi", "starlette", "app.db", "app.repositories", "app.api")
    assert not [m for m in modules if m.startswith(banned)]


def test_sql_lock_constants_match_sign_in_policy() -> None:
    """The threshold and lock length are literals in `app_note_auth_failure` (migration 0011) and
    constants here; this keeps the two equal without a database."""
    import re
    from pathlib import Path

    source = (
        Path(sp.__file__).resolve().parents[2] / "alembic/versions/0011_mfa_lockout.py"
    ).read_text(encoding="utf-8")
    start = source.index("CREATE FUNCTION public.app_note_auth_failure")
    body = source[start : source.index("$$\n", source.index("AS $$", start) + 5)]
    thresholds = set(re.findall(r"failed_auth_count \+ 1 >= (\d+)", body))
    assert thresholds == {str(sp.LOCK_THRESHOLD)}
    assert set(re.findall(r"interval '(\d+) seconds'", body)) == {str(sp.LOCK_SECONDS)}


# ---- ADR-0020: Google sign-in is a first factor only ----

FED = frozenset({"fed"})


@pytest.mark.parametrize("grace", ["unset", "future", "past"])
def test_fed_never_gets_grace(grace: str) -> None:
    f = facts(grace_until=GRACE[grace], has_active_factor=False, first_factor="fed")
    assert sp.in_grace(f) is False
    assert sp.next_step(f, proven=FED) == "mfa_enrollment_required"


def test_fed_with_factor_needs_the_code() -> None:
    f = facts(first_factor="fed")
    assert sp.next_step(f, proven=FED) == "mfa_required"
    assert sp.next_step(f, proven=FED | {"otp"}) == "signed_in"
    assert sp.next_step(f, proven=FED | {"rc"}) == "signed_in"


def test_fed_first_login_enrols() -> None:
    f = facts(first_factor="fed", has_active_factor=False, first_login=True)
    assert sp.next_step(f, proven=FED) == "mfa_enrollment_required"


def test_fed_off_signs_in() -> None:
    assert sp.next_step(facts(enforcement="off", first_factor="fed"), proven=FED) == "signed_in"


@pytest.mark.parametrize(
    "change",
    [
        {"password_must_change": True},
        {"password_changed_at": NOW - timedelta(days=400)},
        {"password_compliant": False},
    ],
)
def test_no_password_step_without_a_password(change: dict[str, object]) -> None:
    f = facts(first_factor="fed", has_password=False, **change)
    assert sp.next_step(f, proven=FED | {"otp"}) == "signed_in"


def test_password_step_still_due_with_a_password() -> None:
    f = facts(first_factor="fed", has_password=True, password_must_change=True)
    assert sp.next_step(f, proven=FED | {"otp"}) == "password_change_required"


def test_linking_never_gets_grace() -> None:
    f = facts(grace_until=GRACE["future"], has_active_factor=False, linking=True)
    assert sp.in_grace(f) is False
    assert sp.next_step(f, proven=PROVEN["pwd"]) == "mfa_enrollment_required"
    assert sp.in_grace(replace(f, linking=False)) is True


@pytest.mark.parametrize(
    ("proven", "first"),
    [
        (frozenset({"fed"}), "fed"),
        (frozenset({"fed", "otp"}), "fed"),
        (frozenset({"pwd"}), "pwd"),
        (frozenset({"pwd", "otp"}), "pwd"),
        (frozenset({"pwd", "fed"}), "pwd"),
        (frozenset(), "pwd"),
    ],
)
def test_first_factor_of(proven: frozenset[str], first: str) -> None:
    assert sp.first_factor_of(proven) == first


# ---- trusted browsers (ADR-0023) ----

DEV = frozenset({"pwd", sp.DEVICE_PROOF})
FED_DEV = frozenset({"fed", sp.DEVICE_PROOF})


def test_device_proof_is_internal_only() -> None:
    assert sp.DEVICE_PROOF == "dev"
    assert sp.DEVICE_PROOF not in sp.SECOND_FACTORS


@pytest.mark.parametrize("proven", [DEV, FED_DEV], ids=["pwd+dev", "fed+dev"])
def test_device_stands_in_for_the_code(proven: frozenset[str]) -> None:
    assert sp.next_step(facts(), proven=proven) == "signed_in"


def test_device_never_satisfies_enrolment() -> None:
    assert sp.next_step(facts(has_active_factor=False), proven=DEV) == "mfa_enrollment_required"
    in_grace = facts(has_active_factor=False, grace_until=GRACE["future"])
    # a device proof never turns a grace sign-in into anything else (no factor, no trust)
    assert sp.next_step(in_grace, proven=DEV) == "signed_in"
    assert sp.trust_usable(in_grace, proven=DEV) is False


@pytest.mark.parametrize(
    "overrides",
    [{"password_must_change": True}, {"password_compliant": False}],
    ids=["must_change", "noncompliant"],
)
def test_device_with_password_change_due_is_not_signed_in(overrides: dict[str, object]) -> None:
    f = facts(**overrides)
    assert sp.next_step(f, proven=DEV) == "password_change_required"
    assert sp.trust_usable(f, proven=DEV) is False


def test_trust_usable_only_when_the_code_is_the_last_thing_missing() -> None:
    assert sp.trust_usable(facts(), proven=DEV) is True
    assert sp.trust_usable(facts(), proven=FED_DEV) is True
    assert sp.trust_usable(facts(enforcement="off"), proven=DEV) is False
    assert sp.trust_usable(facts(linking=True), proven=DEV) is False
    assert sp.trust_usable(facts(is_active=False), proven=DEV) is False


@pytest.mark.parametrize(("admin", "days"), [(False, 30), (True, 7)])
def test_trust_days(admin: bool, days: int) -> None:
    assert sp.trust_days(is_platform_admin=admin) == days


def test_trust_constants() -> None:
    assert (sp.TRUST_DAYS_BRAND, sp.TRUST_DAYS_ADMIN, sp.TRUST_MAX_DEVICES) == (30, 7, 10)


OLD_CODE = NOW - timedelta(days=20)


@pytest.mark.parametrize(
    ("admin", "mfa_at", "device_auth_at", "device_live", "expected"),
    [
        # a device session renews within 7 days of its sign-in while the device is live
        (False, OLD_CODE, NOW - timedelta(days=6, hours=23), True, True),
        (False, OLD_CODE, NOW - timedelta(days=7), True, False),
        (False, OLD_CODE, NOW - timedelta(days=1), False, False),  # removed or expired
        (True, OLD_CODE, NOW - timedelta(days=1), True, True),
        (True, OLD_CODE, NOW - timedelta(days=1), False, False),
        # a step-up after the device sign-in: the normal rule from the fresh code
        (False, NOW - timedelta(days=1), NOW - timedelta(days=2), False, True),
        (False, NOW - timedelta(days=8), NOW - timedelta(days=9), True, False),
        # the code that created the trust is the session's own (mfa_at == device_auth_at)
        (False, NOW - timedelta(days=1), NOW - timedelta(days=1), False, True),
        # no device: unchanged
        (False, NOW - timedelta(days=1), None, False, True),
        (False, OLD_CODE, None, True, False),
    ],
)
def test_refresh_allowed_device_session(
    admin: bool,
    mfa_at: datetime,
    device_auth_at: datetime | None,
    device_live: bool,
    expected: bool,
) -> None:
    assert (
        sp.refresh_allowed(
            enforcement="all",
            grace_until=None,
            is_platform_admin=admin,
            mfa_at=mfa_at,
            now=NOW,
            device_auth_at=device_auth_at,
            device_live=device_live,
        )
        is expected
    )


def test_refresh_allowed_device_session_off_and_grace() -> None:
    old = NOW - timedelta(days=30)
    assert sp.refresh_allowed(
        enforcement="off",
        grace_until=None,
        is_platform_admin=False,
        mfa_at=None,
        now=NOW,
        device_auth_at=old,
        device_live=False,
    )
    assert sp.refresh_allowed(
        enforcement="all",
        grace_until=GRACE["future"],
        is_platform_admin=False,
        mfa_at=None,
        now=NOW,
        device_auth_at=old,
        device_live=False,
    )
