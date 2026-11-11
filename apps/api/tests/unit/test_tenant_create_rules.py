"""ADR-0032: who may add another brand (pure rules), the request body, the operation's contract
and the switch's default."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1.schemas.tenant_create import TenantCreate
from app.api.v1.surface import OPS
from app.services.tenant_create import (
    TRIAL_MAX_OWNED,
    OwnedBrand,
    on_trial,
    refusal,
    trial_end,
)
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError

NOW = datetime(2026, 11, 11, tzinfo=UTC)
ACTIVE: OwnedBrand = ("active", None)


def _trial(days: int) -> OwnedBrand:
    return ("trial", NOW + timedelta(days=days))


def _rule(**overrides: object) -> object:
    args: dict[str, object] = {
        "role": "owner",
        "owned": [ACTIVE],
        "max_owned": 10,
        "suspended_owned": False,
    }
    args.update(overrides)
    return refusal(**args)  # type: ignore[arg-type]


def test_an_owner_of_a_live_brand_may_add_one() -> None:
    assert _rule() is None
    assert _rule(owned=[ACTIVE] * 9) is None
    assert _rule(owned=[_trial(5)]) is None
    assert _rule(owned=[_trial(5)] * (TRIAL_MAX_OWNED - 1)) is None


@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "staff", ""])
def test_only_an_owner(role: str) -> None:
    assert _rule(role=role) == "not_owner"


def test_a_suspended_brand_refuses_before_the_cap() -> None:
    assert _rule(suspended_owned=True) == "suspended"
    assert _rule(suspended_owned=True, owned=[ACTIVE] * 10) == "suspended"
    assert _rule(role="admin", suspended_owned=True) == "not_owner"


def test_the_cap_counts_the_current_brand() -> None:
    assert _rule(owned=[ACTIVE] * 10) == "cap_reached"
    assert _rule(owned=[ACTIVE] * 11) == "cap_reached"
    assert _rule(owned=[ACTIVE], max_owned=1) == "cap_reached"


def test_an_owner_on_trial_has_the_smaller_cap() -> None:
    assert TRIAL_MAX_OWNED * 2.5 < 12  # the tenant and platform AI budgets' defaults
    assert _rule(owned=[_trial(5)] * TRIAL_MAX_OWNED) == "trial_cap_reached"
    # one active brand lifts it
    assert _rule(owned=[ACTIVE] + [_trial(5)] * TRIAL_MAX_OWNED) is None
    # a cap below the trial cap is the plain cap
    assert _rule(owned=[_trial(5)] * 2, max_owned=2) == "cap_reached"
    assert _rule(owned=[_trial(5)] * TRIAL_MAX_OWNED, max_owned=TRIAL_MAX_OWNED) == "cap_reached"


def test_on_trial_until_one_brand_is_active() -> None:
    assert on_trial([_trial(1)]) and on_trial([])
    assert not on_trial([_trial(1), ACTIVE])


def test_a_trial_owner_keeps_their_latest_trial_end() -> None:
    assert trial_end([_trial(1)]) == NOW + timedelta(days=1)
    assert trial_end([_trial(1), _trial(20), _trial(3)]) == NOW + timedelta(days=20)
    assert trial_end([("trial", None), _trial(2)]) == NOW + timedelta(days=2)


def test_a_fresh_trial_only_without_an_owned_trial_end_or_with_an_active_brand() -> None:
    assert trial_end([ACTIVE]) is None
    assert trial_end([ACTIVE, _trial(1)]) is None
    assert trial_end([("trial", None)]) is None
    assert trial_end([]) is None


def test_body_is_strict_and_validated() -> None:
    ok = TenantCreate.model_validate({"name": "Kalakar Home", "website": "https://kalakar.in"})
    assert ok.name == "Kalakar Home" and ok.website == "https://kalakar.in"
    assert TenantCreate.model_validate({"name": "Plain"}).website is None
    for bad in (
        {},
        {"name": " "},
        {"name": "x" * 201},
        {"name": "A\x00B"},
        {"name": "Ok", "website": "kalakar.in"},
        {"name": "Ok", "role": "owner"},
        {"name": "Ok", "trial_days": 90},
    ):
        with pytest.raises(ValidationError):
            TenantCreate.model_validate(bad)


def test_operation_contract() -> None:
    op = OPS["tenants_create"]
    assert (op.method, op.path, op.status) == ("POST", "/tenants", 201)
    assert op.idempotency and op.step_up == "any" and op.has_body
    assert set(op.roles or ()) == {"owner", "admin"}
    codes = op.error_codes
    assert codes[403] == (
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.MFA_STEP_UP_REQUIRED,
        ErrorCode.MFA_ENROLLMENT_REQUIRED,
    )
    assert codes[409] == (ErrorCode.IDEMPOTENCY_KEY_REUSED,)
    assert codes[501] == (ErrorCode.NOT_IMPLEMENTED,)


def test_switch_is_off_and_cap_is_ten_by_default() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["tenant_create_enabled"].default is False
    assert Settings.model_fields["tenant_create_max_owned"].default == 10
