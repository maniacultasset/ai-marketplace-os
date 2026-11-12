"""S10 C2a (DW2, AC2, AC10; CODE-PLAN Q7): every operation's roles are derived from its action and
the one matrix, so the 403s the contract declares and the case-matrix guard asks for match what
`require()` enforces at runtime."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api.v1.surface import ALL_ROLES, OPS, cases
from app.core.rbac import MATRIX, PLATFORM_ADMIN, PUBLIC, Action
from tests.conftest import REPO_ROOT

# CODE-PLAN Q7, written out: the action each operation requires.
Q7: dict[str, str] = {
    "auth_login": PUBLIC,
    "auth_refresh": PUBLIC,
    "auth_logout": PUBLIC,
    "auth_me": "tenant:read",
    "auth_switch_tenant": "tenant:read",
    # DPP-1: the challenge steps are public (the challenge is checked in the service); the
    # bearer MFA and password routes need only a live membership
    "auth_login_mfa": PUBLIC,
    "auth_login_mfa_enroll": PUBLIC,
    "auth_login_mfa_confirm": PUBLIC,
    "auth_password_expired": PUBLIC,
    "auth_mfa_totp_enroll": "tenant:read",
    "auth_mfa_totp_confirm": "tenant:read",
    "auth_security": "tenant:read",
    "auth_mfa_recovery_codes": "tenant:read",
    "auth_mfa_step_up": "tenant:read",
    "auth_password_change": "tenant:read",
    # ADR-0020: Google sign-in (the JSON steps are public: the handle is the credential)
    "auth_google_config": PUBLIC,
    "auth_google_start": PUBLIC,
    "auth_google_callback": PUBLIC,
    "auth_google_complete": PUBLIC,
    "auth_google_link": PUBLIC,
    "auth_google_unlink": "tenant:read",
    # ADR-0023: the caller's own trusted browsers
    "auth_devices_list": "tenant:read",
    "auth_devices_remove": "tenant:read",
    "auth_devices_sign_out_all": "tenant:read",
    "tenants_me": "tenant:read",
    "tenants_me_update": "settings:manage",
    "brand_documents_upload": "settings:manage",
    "brand_documents_list": "tenant:read",
    "brand_documents_delete": "settings:manage",
    "brand_profile_get": "tenant:read",
    "brand_profile_update": "settings:manage",
    "products_create": "product:import",
    "products_list": "tenant:read",
    "products_get": "tenant:read",
    "products_update": "product:import",
    "products_recategorize": "product:import",  # SI9
    "products_bulk": "product:import",
    "products_delete": "settings:manage",  # PD1
    "products_bulk_delete": "settings:manage",  # PD1
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
    "listings_versions": "tenant:read",  # item 17
    "issues_list": "tenant:read",
    "issues_dismiss": "listing:approve",
    "tasks_list": "tenant:read",
    "tasks_answer": "product:import",
    "tasks_close": "marketplace:connect",  # SI9
    "listings_content_patch": "listing:edit",
    "listings_regenerate": "listing:regenerate",
    "listings_retry_fields": "listing:publish",
    "listings_submissions": "tenant:read",  # S21 CP7
    "listings_resolution": "tenant:read",  # SI11 T20
    "listings_issue_history": "tenant:read",  # feedback item 20
    "listings_refresh_status": "listing:publish",  # S21 CP7
    "listings_fixes_apply": "listing:edit",  # client item 20
    "marketplaces_connect": "marketplace:connect",
    "marketplaces_callback": PUBLIC,
    "marketplaces_accounts": "tenant:read",
    "marketplaces_connect_confirm": "marketplace:connect",
    "marketplaces_accounts_revoke": "marketplace:connect",
    "images_upload_url": "product:import",
    "images_upload": "product:import",
    "images_report": "tenant:read",
    "images_delete": "product:import",
    "image_candidates_create": "product:import",
    "size_chart_read_create": "product:import",  # SI12
    "products_category_suggest": "product:import",  # SI14
    "products_size_run_create": "product:import",  # V1.1
    "images_photo_match_keep": "product:import",  # V1.1
    "tenants_create": "settings:manage",  # ADR-0032 (the service also needs owner)
    "imports_column_map_create": "product:import",  # SI14
    "imports_column_map_get": "tenant:read",  # SI14
    "imports_column_map_confirm": "product:import",  # SI14
    "catalog_requirements_get": "tenant:read",  # SI12
    # client feedback 11 Nov
    "catalog_categories_list": "tenant:read",
    "catalog_category_suggest": "product:import",
    "catalog_category_suggestion_get": "tenant:read",
    "skus_availability": "tenant:read",
    "skus_suggest": "product:import",
    "image_candidates_list": "tenant:read",
    "image_candidates_accept": "product:import",
    "image_candidates_reject": "product:import",
    "rules_list": "tenant:read",
    "rules_dry_run": "listing:edit",
    "jobs_get": "tenant:read",
    "jobs_cancel": "listing:generate",
    "analytics_get": "analytics:read",
    "analytics_summary": "analytics:read",
    "analytics_quality": "analytics:read",  # SI14
    "home_summary": "tenant:read",  # FB6
    "members_list": "team:manage",
    "members_patch": "team:manage",
    # ADR-0019: the public sign-up and the platform admin's queue (no role in the matrix)
    "signup_create": PUBLIC,
    "signup_status": PUBLIC,
    "signup_status_poll": PUBLIC,
    "signup_status_reply": PUBLIC,
    "signup_google": PUBLIC,
    "admin_signup_requests_list": PLATFORM_ADMIN,
    "admin_signup_requests_approve": PLATFORM_ADMIN,
    "admin_signup_requests_reject": PLATFORM_ADMIN,
    "admin_signup_requests_ask": PLATFORM_ADMIN,
    # SI5 (ADR-0029)
    "invites_create": "team:manage",
    "invites_list": "team:manage",
    "invites_revoke": "team:manage",
    "invites_lookup": PUBLIC,
    "invites_accept": PUBLIC,
    "invites_join": "tenant:read",
    "members_password_reset": "team:manage",
    "auth_password_reset": PUBLIC,
    "admin_tenants_list": PLATFORM_ADMIN,
    "admin_tenants_activate": PLATFORM_ADMIN,
    "admin_tenants_extend_trial": PLATFORM_ADMIN,
    "admin_ai_credit_get": PLATFORM_ADMIN,  # SI10
    "admin_quality": PLATFORM_ADMIN,  # SI14
    "admin_ai_credit_resume": PLATFORM_ADMIN,
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
    # S10 38 + S15 two + S16 three + S20 preview + S19 four + S21 two + ADR-0019 five
    # + DPP-1 ten + ADR-0020 seven + ADR-0023 three + SI1 four + SI3 one + SI5 eleven + SI7 four
    # + SI9 two
    # + SI10 two + SI11 one + SI12 two + SI14 (the V1.1 assist)
    assert len(OPS) == len(Q7)
    assert set(OPS) == set(Q7)


@pytest.mark.parametrize("op_id", sorted(Q7))
def test_ops_roles_equal_matrix_for_action(op_id: str) -> None:
    op = OPS[op_id]
    if Q7[op_id] == PUBLIC:
        assert op.public and op.action is None
        assert op.roles == ALL_ROLES
        return
    if Q7[op_id] == PLATFORM_ADMIN:
        assert op.platform_admin and op.action is None
        assert op.roles == frozenset()
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


# DPP-1: every role may call them, but a step-up refusal is a 403 of its own (no PERMISSION_DENIED)
MFA_403 = frozenset(
    {
        "auth_mfa_totp_enroll",
        "auth_mfa_totp_confirm",
        "auth_mfa_recovery_codes",
        "auth_google_unlink",  # ADR-0020: a step-up first
    }
)


@pytest.mark.parametrize(
    "op_id", sorted(op for op, a in Q7.items() if a in {"tenant:read", "analytics:read"})
)
def test_every_role_ops_declare_no_403(op_id: str) -> None:
    responses = _operation(_spec(), op_id)["responses"]
    if op_id in MFA_403:
        example = responses["403"]["content"]["application/json"]["example"]
        assert example["error"]["type"] == "MFA_STEP_UP_REQUIRED", op_id
        return
    assert "403" not in responses, op_id


@pytest.mark.parametrize("op_id", RESTRICTED)
def test_cases_emit_wrong_role_for_restricted(op_id: str) -> None:
    assert "wrong_role" in cases(OPS[op_id])


@pytest.mark.parametrize("op_id", sorted(Q7))
def test_every_operation_has_x_mkos_action(op_id: str) -> None:
    assert _operation(_spec(), op_id)["x-mkos-action"] == Q7[op_id]
