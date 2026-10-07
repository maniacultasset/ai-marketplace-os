"""S3 T6 (AC1, AC5, AC9; D1, D9, D10): the frozen `/api/v1` operation registry."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from app.api.v1.surface import (
    ALL_ROLES,
    CASES,
    OPS,
    Operation,
    cases,
    declared_responses,
)
from app.core.rbac import Action
from mkos_schemas.codes import ErrorCode

# The frozen list (PLAN D1): 35 operations at S3 plus S10's switch and members (38), no
# dead-letter path. Later steps add ops as
# "merged literal + N" (S10 38, S15 40, S16 43, S19 47, S20 48, S21 50).
FROZEN: tuple[tuple[str, str, str], ...] = (
    ("auth_login", "POST", "/auth/login"),
    ("auth_refresh", "POST", "/auth/refresh"),
    ("auth_logout", "POST", "/auth/logout"),
    ("auth_me", "GET", "/auth/me"),
    ("auth_switch_tenant", "POST", "/auth/switch-tenant"),
    ("members_list", "GET", "/members"),
    ("members_patch", "PATCH", "/members/{user_id}"),
    ("tenants_me", "GET", "/tenants/me"),
    # S16: the profile update, the document list and delete (40 + 3)
    ("tenants_me_update", "PATCH", "/tenants/me"),
    ("brand_documents_upload", "POST", "/brand/documents"),
    ("brand_documents_list", "GET", "/brand/documents"),
    ("brand_documents_delete", "DELETE", "/brand/documents/{document_id}"),
    ("products_create", "POST", "/products"),
    ("products_list", "GET", "/products"),
    ("products_get", "GET", "/products/{product_id}"),
    ("products_bulk", "POST", "/products/bulk"),
    ("imports_get", "GET", "/imports/{import_id}"),
    ("imports_rows", "GET", "/imports/{import_id}/rows"),
    ("imports_rows_retry", "POST", "/imports/{import_id}/rows/retry"),
    ("listings_generate", "POST", "/listings/generate"),
    ("listings_validate", "POST", "/listings/validate"),
    ("listings_approve", "POST", "/listings/approve"),
    ("listings_publish", "POST", "/listings/publish"),
    ("listings_list", "GET", "/listings"),
    ("listings_get", "GET", "/listings/{listing_id}"),
    ("listings_status", "GET", "/listings/{listing_id}/status"),
    ("listings_content_patch", "PATCH", "/listings/{listing_id}/content"),
    ("listings_regenerate", "POST", "/listings/{listing_id}/regenerate"),
    ("listings_retry_fields", "POST", "/listings/{listing_id}/publish/retry-fields"),
    ("marketplaces_connect", "POST", "/marketplaces/connect"),
    ("marketplaces_callback", "GET", "/marketplaces/{marketplace}/callback"),
    ("marketplaces_accounts", "GET", "/marketplaces/accounts"),
    # S15: the confirm and the revoke (38 + 2)
    ("marketplaces_connect_confirm", "POST", "/marketplaces/connect/confirm"),
    ("marketplaces_accounts_revoke", "POST", "/marketplaces/accounts/{account_id}/revoke"),
    ("images_upload_url", "POST", "/images/upload-url"),
    ("images_upload", "POST", "/images/upload"),
    ("images_report", "GET", "/images/{image_id}/report"),
    ("rules_list", "GET", "/rules"),
    ("rules_dry_run", "POST", "/rules/dry-run"),
    ("jobs_get", "GET", "/jobs/{job_id}"),
    ("jobs_cancel", "POST", "/jobs/{job_id}/cancel"),
    ("analytics_get", "GET", "/analytics"),
    ("analytics_summary", "GET", "/analytics/summary"),
)

E = ErrorCode


def test_registry_equals_frozen_list() -> None:
    assert len(FROZEN) == 43
    assert tuple((op.op_id, op.method, op.path) for op in OPS.values()) == FROZEN
    assert all(key == op.op_id for key, op in OPS.items())
    assert not any("dead-letter" in op.path for op in OPS.values())


def test_paths_prefixless_and_unique() -> None:
    seen: set[tuple[str, str]] = set()
    for op in OPS.values():
        assert op.path.startswith("/"), op.op_id
        assert not op.path.startswith("/api"), op.op_id
        assert not op.path.endswith("/"), op.op_id
        assert (op.method, op.path) not in seen, op.op_id
        seen.add((op.method, op.path))


def _op(**changes: Any) -> Operation:
    base = Operation(
        op_id="x_op",
        method="GET",
        path="/x",
        status=200,
        response_model=None,
        group="x",
        summary="x",
        owner_step="S99",
        action=Action.TENANT_READ,
    )
    return dataclasses.replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "present", "absent"),
    [
        (
            {},
            {"success", "missing_auth", "invalid_token", "wrong_tenant"},
            {"validation", "wrong_role", "not_found", "conflict"},
        ),
        (
            {"public": True, "action": None, "roles": frozenset()},
            {"success"},
            {"missing_auth", "invalid_token", "wrong_tenant", "wrong_role"},
        ),
        ({"query": ("limit",)}, {"validation"}, set()),
        ({"has_body": True, "method": "POST"}, {"validation"}, set()),
        ({"path": "/x/{x_id}", "path_id": "x_id"}, {"validation", "not_found"}, set()),
        ({"has_body": True, "body_ids": ("images",)}, {"not_found"}, set()),
        ({"roles": frozenset({"owner"})}, {"wrong_role"}, set()),
        ({"idempotency": True}, {"conflict", "validation"}, set()),
        ({"if_match": True, "conflict_code": E.STALE_VERSION}, {"conflict", "validation"}, set()),
        ({"maker_checker": True}, {"maker_checker"}, set()),
        (
            {"oauth_state": True, "public": True, "action": None, "roles": frozenset()},
            {"bad_state"},
            {"conflict"},
        ),
    ],
)
def test_case_rules(changes: dict[str, Any], present: set[str], absent: set[str]) -> None:
    got = cases(_op(**changes))
    assert present <= got
    assert not (absent & got)
    assert got <= set(CASES)


def test_maker_checker_and_bad_state_rows() -> None:
    assert "maker_checker" in cases(OPS["listings_approve"])
    assert "bad_state" in cases(OPS["marketplaces_callback"])
    assert [o.op_id for o in OPS.values() if "maker_checker" in cases(o)] == ["listings_approve"]
    assert [o.op_id for o in OPS.values() if "bad_state" in cases(o)] == [
        "marketplaces_callback",
        "marketplaces_connect_confirm",
    ]


def test_confirm_and_revoke_case_rows() -> None:
    """S15: the confirm is a mutating, authenticated consume of the pending handle (its 409s are
    the seller conflict and the uniform bad handle); the revoke is a path-id write with no key
    (naturally idempotent)."""
    assert cases(OPS["marketplaces_connect_confirm"]) == {
        "success",
        "validation",
        "missing_auth",
        "invalid_token",
        "wrong_tenant",
        "wrong_role",
        "conflict",
        "bad_state",
    }
    assert cases(OPS["marketplaces_accounts_revoke"]) == {
        "success",
        "validation",
        "missing_auth",
        "invalid_token",
        "wrong_tenant",
        "wrong_role",
        "not_found",
    }
    assert not OPS["marketplaces_accounts_revoke"].idempotency


def test_storage_key_body_fields_count_as_body_ids() -> None:
    assert "images" in OPS["products_create"].body_ids
    assert "storage_key" in OPS["images_upload"].body_ids
    for op_id in ("products_create", "images_upload"):
        assert {"not_found", "wrong_tenant"} <= cases(OPS[op_id])


D9_IDEMPOTENT = {
    "products_bulk",
    "imports_rows_retry",
    "listings_generate",
    "listings_approve",
    "listings_publish",
    "listings_regenerate",
    "listings_retry_fields",
    "jobs_cancel",
    "brand_documents_upload",
    "images_upload",
}


def test_idempotency_set_is_d9() -> None:
    assert {o.op_id for o in OPS.values() if o.idempotency} == D9_IDEMPOTENT
    assert {o.op_id for o in OPS.values() if o.if_match} == {
        "listings_content_patch",
        "tenants_me_update",
    }


# S3's `test_roles_are_d10` is replaced by S10's `tests/unit/test_ops_roles.py`: the roles are
# derived from each op's action through the matrix (CODE-PLAN S10 C2a).


def test_public_and_action_go_together() -> None:
    with pytest.raises(ValueError, match="public op has no action"):
        _op(public=True)
    with pytest.raises(ValueError, match="public op has no action"):
        _op(action=None)


def test_public_ops() -> None:
    assert {o.op_id for o in OPS.values() if o.public} == {
        "auth_login",
        "auth_refresh",
        "auth_logout",
        "marketplaces_callback",
    }


def test_error_codes_per_d2() -> None:
    conflict = {
        "products_create": (E.SKU_CONFLICT,),
        "listings_content_patch": (E.STALE_VERSION,),
        "tenants_me_update": (E.STALE_VERSION,),
        "jobs_cancel": (E.IDEMPOTENCY_KEY_REUSED, E.INVALID_STATUS_TRANSITION),
        "imports_rows_retry": (E.IDEMPOTENCY_KEY_REUSED, E.INVALID_STATUS_TRANSITION),
        "marketplaces_callback": (E.OAUTH_STATE_INVALID,),
        "marketplaces_connect_confirm": (E.MARKETPLACE_ACCOUNT_CONFLICT, E.OAUTH_STATE_INVALID),
        "members_patch": (E.LAST_OWNER_REQUIRED,),
    }
    for op in OPS.values():
        codes = op.error_codes
        c = cases(op)
        assert codes[429] == (E.RATE_LIMITED,)
        if "missing_auth" in c or op.op_id in {"auth_login", "auth_refresh"}:
            assert codes[401] == (E.UNAUTHENTICATED,), op.op_id
        else:
            assert 401 not in codes, op.op_id
        if op.op_id == "listings_approve":
            assert codes[403] == (E.PERMISSION_DENIED, E.MAKER_CHECKER_VIOLATION)
        elif "wrong_role" in c:
            assert codes[403] == (E.PERMISSION_DENIED,), op.op_id
        else:
            assert 403 not in codes, op.op_id
        if "not_found" in c:
            assert codes[404] == (E.NOT_FOUND,), op.op_id
        else:
            assert 404 not in codes, op.op_id
        if op.op_id in conflict:
            assert codes[409] == conflict[op.op_id], op.op_id
        elif op.idempotency:
            assert codes[409] == (E.IDEMPOTENCY_KEY_REUSED,), op.op_id
        else:
            assert 409 not in codes, op.op_id
        assert (413 in codes) is op.has_body, op.op_id
        if op.has_body:
            assert codes[413] == (E.PAYLOAD_TOO_LARGE,)
        if "validation" in c:
            extra_422 = {
                "products_create": (E.UNKNOWN_FACT_KEY,),
                "products_bulk": (E.IMPORT_FAILED,),  # S13b: the file-level import report
            }
            expected_422 = (E.VALIDATION_FAILED, *extra_422.get(op.op_id, ()))
            assert codes[422] == expected_422, op.op_id
        else:
            assert 422 not in codes, op.op_id


def test_owner_steps_known() -> None:
    allowed = {"S10", "S12", "S13", "S14b", "S15", "S16", "S17b", "S18", "S19", "S21", "S23"}
    for op in OPS.values():
        assert op.owner_step in allowed, op.op_id


def test_wrong_role_and_403_follow_roles() -> None:
    base = OPS["rules_list"]  # every role holds tenant:read
    assert base.roles == ALL_ROLES
    assert "wrong_role" not in cases(base)
    assert 403 not in declared_responses(base)
    narrowed = dataclasses.replace(base, roles=frozenset({"owner"}))
    assert "wrong_role" in cases(narrowed)
    responses = declared_responses(narrowed)
    assert 403 in responses
    example = responses[403]["content"]["application/json"]["example"]
    assert example["error"]["type"] == E.PERMISSION_DENIED.value
    widened = dataclasses.replace(narrowed, roles=ALL_ROLES)
    assert "wrong_role" not in cases(widened)
    assert 403 not in declared_responses(widened)


def test_declared_responses_examples_use_first_code() -> None:
    for op in OPS.values():
        responses = declared_responses(op)
        assert "default" in responses
        for status, codes in op.error_codes.items():
            example = responses[status]["content"]["application/json"]["example"]
            assert example["error"]["type"] == codes[0].value
            assert example["error"]["status"] == status
