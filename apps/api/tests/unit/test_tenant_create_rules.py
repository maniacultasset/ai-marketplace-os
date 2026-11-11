"""ADR-0032: who may add another brand (pure rules), the request body, the operation's contract
and the switch's default."""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.schemas.tenant_create import TenantCreate
from app.api.v1.surface import OPS
from app.repositories.identity import MembershipRow
from app.services.tenant_create import owned_live, refusal
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError


def _rule(**overrides: object) -> object:
    args: dict[str, object] = {
        "role": "owner",
        "owned_live": 1,
        "max_owned": 10,
        "suspended_elsewhere": False,
    }
    args.update(overrides)
    return refusal(**args)  # type: ignore[arg-type]


def test_an_owner_of_a_live_brand_may_add_one() -> None:
    assert _rule() is None
    assert _rule(owned_live=9) is None


@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "staff", ""])
def test_only_an_owner(role: str) -> None:
    assert _rule(role=role) == "not_owner"


def test_a_suspended_brand_refuses_before_the_cap() -> None:
    assert _rule(suspended_elsewhere=True) == "suspended"
    assert _rule(suspended_elsewhere=True, owned_live=10) == "suspended"
    assert _rule(role="admin", suspended_elsewhere=True) == "not_owner"


def test_the_cap_counts_the_current_brand() -> None:
    assert _rule(owned_live=10) == "cap_reached"
    assert _rule(owned_live=11) == "cap_reached"
    assert _rule(owned_live=1, max_owned=1) == "cap_reached"


def _member(role: str) -> MembershipRow:
    return MembershipRow(
        tenant_id=uuid.uuid4(),
        tenant_name="Brand",
        user_id=uuid.uuid4(),
        email="a@b.co",
        display_name=None,
        role=role,
        last_used_at=None,
    )


def test_owned_live_counts_owner_rows_only() -> None:
    rows = [_member("owner"), _member("operator"), _member("owner"), _member("admin")]
    assert owned_live(rows) == 2
    assert owned_live([]) == 0


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
