"""S10 C3 (AC10; CODE-PLAN S10 §5): the switch and members contract, and one version everywhere."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest
from app.api.v1.schemas.auth import (
    MemberOut,
    MemberPage,
    MemberPatch,
    MembershipOut,
    SwitchTenantRequest,
)
from app.api.v1.surface import OPS
from mkos_schemas.base import SCHEMA_VERSION
from mkos_schemas.codes import ErrorCode
from pydantic import ValidationError
from tests.conftest import REPO_ROOT

SCHEMAS = REPO_ROOT / "packages/schemas"


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads((SCHEMAS / "jsonschema/openapi.json").read_text("utf-8"))
    return spec


def test_auth_contract_models() -> None:
    target = uuid.uuid4()
    assert (
        SwitchTenantRequest.model_validate({"target_tenant_id": str(target)}).target_tenant_id
        == target
    )
    for bad in (
        {},
        {"tenant_id": str(target)},
        {"target_tenant_id": "x"},
        {"target_tenant_id": str(target), "x": 1},
    ):
        with pytest.raises(ValidationError):
            SwitchTenantRequest.model_validate(bad)
    member = MemberOut.model_validate(
        {"user_id": str(target), "email": "a@b.co", "role": "reviewer", "is_active": True}
    )
    assert member.display_name is None
    page = MemberPage.model_validate(
        {"data": [member.model_dump(mode="json")], "page": {"has_more": False, "limit": 50}}
    )
    assert page.data == [member]
    assert (
        MembershipOut.model_validate(
            {"tenant_id": str(target), "tenant_name": "Acme", "role": "owner"}
        ).role
        == "owner"
    )
    assert MemberPatch.model_validate({"role": "operator"}).is_active is None
    assert MemberPatch.model_validate({"is_active": False}).is_active is False


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"role": None},
        {"is_active": None},
        {"role": None, "is_active": None},
        {"role": "superuser"},
        {"is_active": "false"},
        {"is_active": 0},
        {"role": "owner", "tenant_id": str(uuid.UUID(int=1))},
    ],
)
def test_member_patch_empty_rejected(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MemberPatch.model_validate(body)


def test_new_operations_registered() -> None:
    switch, members, patch = OPS["auth_switch_tenant"], OPS["members_list"], OPS["members_patch"]
    assert (switch.method, switch.path, switch.status) == ("POST", "/auth/switch-tenant", 200)
    assert (members.method, members.path) == ("GET", "/members")
    assert (patch.method, patch.path, patch.path_id) == ("PATCH", "/members/{user_id}", "user_id")
    assert patch.error_codes[409] == (ErrorCode.LAST_OWNER_REQUIRED,)
    assert switch.error_codes[404] == (ErrorCode.NOT_FOUND,)
    spec = _spec()
    conflict = spec["paths"]["/api/v1/members/{user_id}"]["patch"]["responses"]["409"]
    assert conflict["content"]["application/json"]["example"]["error"]["type"] == (
        "LAST_OWNER_REQUIRED"
    )
    for name in ("SwitchTenantRequest", "MemberOut", "MemberPage", "MemberPatch", "MembershipOut"):
        assert name in spec["components"]["schemas"], name


def test_dpp1_operations_registered() -> None:
    """DPP-1 (2.0.0): the 10 MFA, step-up and password operations and their models."""
    paths = {
        "auth_login_mfa": ("POST", "/auth/login/mfa"),
        "auth_login_mfa_enroll": ("POST", "/auth/login/mfa/enroll"),
        "auth_login_mfa_confirm": ("POST", "/auth/login/mfa/confirm"),
        "auth_password_expired": ("POST", "/auth/password/expired"),
        "auth_mfa_totp_enroll": ("POST", "/auth/mfa/totp/enroll"),
        "auth_mfa_totp_confirm": ("POST", "/auth/mfa/totp/confirm"),
        "auth_security": ("GET", "/auth/security"),
        "auth_mfa_recovery_codes": ("POST", "/auth/mfa/recovery-codes"),
        "auth_mfa_step_up": ("POST", "/auth/mfa/step-up"),
        "auth_password_change": ("POST", "/auth/password"),
    }
    for op_id, (method, path) in paths.items():
        assert (OPS[op_id].method, OPS[op_id].path) == (method, path), op_id
    spec = _spec()
    for name in (
        "LoginResult",
        "LoginMfaRequest",
        "LoginEnrollRequest",
        "LoginConfirmRequest",
        "TotpEnrollRequest",
        "TotpConfirmRequest",
        "StepUpRequest",
        "PasswordChangeRequest",
        "PasswordExpiredRequest",
        "TotpEnrollOut",
        "LoginConfirmOut",
        "TotpConfirmOut",
        "SecurityOut",
        "RecoveryCodesOut",
        "StepUpOut",
    ):
        assert name in spec["components"]["schemas"], name
    login = spec["paths"]["/api/v1/auth/login"]["post"]["responses"]["200"]
    assert login["content"]["application/json"]["schema"]["$ref"].endswith("/LoginResult")


def _version(path: str, pattern: str) -> str:
    found = re.search(pattern, (SCHEMAS / path).read_text("utf-8"), re.M)
    assert found is not None, path
    return found.group(1)


def test_schema_version_consistent() -> None:
    versions = {
        "pyproject": _version("pyproject.toml", r'^version = "([^"]+)"$'),
        "package.json": json.loads((SCHEMAS / "ts/package.json").read_text("utf-8"))["version"],
        "openapi info.version": _spec()["info"]["version"],
        "SCHEMA_VERSION": SCHEMA_VERSION,
    }
    for fixture in sorted((SCHEMAS / "fixtures").glob("*.json")):
        data = json.loads(fixture.read_text("utf-8"))
        if isinstance(data, dict) and "schema_version" in data:
            versions[fixture.name] = data["schema_version"]
    assert len(versions) > 4
    assert set(versions.values()) == {SCHEMA_VERSION}, versions
    changelog = (SCHEMAS / "CHANGELOG.md").read_text("utf-8")
    assert re.search(rf"^## {re.escape(SCHEMA_VERSION)}\b", changelog, re.M)
