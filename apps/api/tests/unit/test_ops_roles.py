"""S10 C2a (DW2, AC2, AC10; CODE-PLAN Q7): every operation's roles are derived from its action and
the one matrix, so the 403s the contract declares and the case-matrix guard asks for match what
`require()` enforces at runtime."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api.v1.surface import ALL_ROLES, OPS, cases
from app.core.rbac import MATRIX, PUBLIC, Action
from tests.conftest import REPO_ROOT

# CODE-PLAN Q7, written out: the action each operation requires.
Q7: dict[str, str] = {
    "auth_login": PUBLIC,
    "auth_refresh": PUBLIC,
    "auth_logout": PUBLIC,
    "auth_me": "tenant:read",
    "auth_switch_tenant": "tenant:read",
    "tenants_me": "tenant:read",
    "tenants_me_update": "settings:manage",
    "brand_documents_upload": "settings:manage",
    "brand_documents_list": "tenant:read",
    "brand_documents_delete": "settings:manage",
    "products_create": "product:import",
    "products_list": "tenant:read",
    "products_get": "tenant:read",
    "products_bulk": "product:import",
    "imports_get": "tenant:read",
    "imports_rows": "tenant:read",
    "imports_rows_retry": "product:import",
    "listings_generate": "listing:generate",
    "listings_validate": "listing:edit",
    "listings_approve": "listing:approve",
    "listings_publish": "listing:publish",
    "listings_list": "tenant:read",
    "listings_get": "tenant:read",
    "listings_preview": "tenant:read",
    "listings_status": "tenant:read",
    "listings_history": "tenant:read",
    "issues_list": "tenant:read",
    "issues_dismiss": "listing:approve",
    "tasks_list": "tenant:read",
    "listings_content_patch": "listing:edit",
    "listings_regenerate": "listing:regenerate",
    "listings_retry_fields": "listing:publish",
    "listings_submissions": "tenant:read",  # S21 CP7
    "listings_refresh_status": "listing:publish",  # S21 CP7
    "marketplaces_connect": "marketplace:connect",
    "marketplaces_callback": PUBLIC,
    "marketplaces_accounts": "tenant:read",
    "marketplaces_connect_confirm": "marketplace:connect",
    "marketplaces_accounts_revoke": "marketplace:connect",
    "images_upload_url": "product:import",
    "images_upload": "product:import",
    "images_report": "tenant:read",
    "rules_list": "tenant:read",
    "rules_dry_run": "listing:edit",
    "jobs_get": "tenant:read",
    "jobs_cancel": "listing:generate",
    "analytics_get": "analytics:read",
    "analytics_summary": "analytics:read",
    "members_list": "team:manage",
    "members_patch": "team:manage",
}
NARROWED_BY_S10 = (
    "images_upload_url",
    "images_upload",
    "listings_validate",
    "rules_dry_run",
    "jobs_cancel",
    "brand_documents_upload",
)
OPENAPI = REPO_ROOT / "packages/schemas/jsonschema/openapi.json"


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(OPENAPI.read_text(encoding="utf-8"))
    return spec


def _operation(spec: dict[str, Any], op_id: str) -> dict[str, Any]:
    op = OPS[op_id]
    operation: dict[str, Any] = spec["paths"][f"/api/v1{op.path}"][op.method.lower()]
    return operation


def test_ops_are_the_q7_list() -> None:
    assert len(OPS) == 50  # S10 38 + S15 two + S16 three + S20 preview + S19 four + S21 two
    assert set(OPS) == set(Q7)


@pytest.mark.parametrize("op_id", sorted(Q7))
def test_ops_roles_equal_matrix_for_action(op_id: str) -> None:
    op = OPS[op_id]
    if Q7[op_id] == PUBLIC:
        assert op.public and op.action is None
        assert op.roles == ALL_ROLES
        return
    assert not op.public
    assert op.action == Action(Q7[op_id])
    assert op.roles == frozenset(r for r in MATRIX if op.action in MATRIX[r])


RESTRICTED = sorted(
    op_id for op_id, action in Q7.items() if action not in {PUBLIC, "tenant:read", "analytics:read"}
)


def test_restricted_set_includes_the_s10_narrowed_ops() -> None:
    assert set(NARROWED_BY_S10) <= set(RESTRICTED)


@pytest.mark.parametrize("op_id", RESTRICTED)
def test_restricted_ops_declare_403(op_id: str) -> None:
    responses = _operation(_spec(), op_id)["responses"]
    assert "403" in responses, op_id
    example = responses["403"]["content"]["application/json"]["example"]
    assert example["error"]["type"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "op_id", sorted(op for op, a in Q7.items() if a in {"tenant:read", "analytics:read"})
)
def test_every_role_ops_declare_no_403(op_id: str) -> None:
    assert "403" not in _operation(_spec(), op_id)["responses"], op_id


@pytest.mark.parametrize("op_id", RESTRICTED)
def test_cases_emit_wrong_role_for_restricted(op_id: str) -> None:
    assert "wrong_role" in cases(OPS[op_id])


@pytest.mark.parametrize("op_id", sorted(Q7))
def test_every_operation_has_x_mkos_action(op_id: str) -> None:
    assert _operation(_spec(), op_id)["x-mkos-action"] == Q7[op_id]
