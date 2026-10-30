"""Success examples for every frozen operation (S3 AC12). Fixed ids and timestamps, so the export is
deterministic. They land in openapi.json, the FE reads them, and the local mock serves them
(`app_mock`). Each one validates against its op's response model (`test_examples_validate`)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

T0: Final = "2026-10-05T09:30:00Z"
T1: Final = "2026-10-05T09:31:00Z"
TENANT: Final = "0b1c2d3e-0000-4000-8000-000000000001"
USER: Final = "0b1c2d3e-0000-4000-8000-000000000002"
USER_2: Final = "0b1c2d3e-0000-4000-8000-000000000003"
PRODUCT_1: Final = "0b1c2d3e-0000-4000-8000-000000000101"
PRODUCT_2: Final = "0b1c2d3e-0000-4000-8000-000000000102"
LISTING: Final = "0b1c2d3e-0000-4000-8000-000000000201"
VERSION: Final = "0b1c2d3e-0000-4000-8000-000000000202"
JOB: Final = "0b1c2d3e-0000-4000-8000-000000000301"
IMPORT: Final = "0b1c2d3e-0000-4000-8000-000000000401"
IMAGE: Final = "0b1c2d3e-0000-4000-8000-000000000501"
ACCOUNT: Final = "0b1c2d3e-0000-4000-8000-000000000601"
RULE: Final = "0b1c2d3e-0000-4000-8000-000000000701"
BATCH: Final = "0b1c2d3e-0000-4000-8000-000000000801"
REQUEST_ID: Final = "req-example-0001"
ETAG_EXAMPLE: Final = '"5f0c7c1e-8d2b-5a4e-9b6f-3c2d1e0f9a8b"'
DOCUMENT: Final = "0b1c2d3e-0000-4000-8000-000000000901"
CALLBACK_LOCATION: Final = "/marketplaces/connected?pending=pnd_8f3a2c1e9b7d4f60a1c2"
# ADR-0020: Google sign-in's two redirects (no real client id, state or handle)
GOOGLE_AUTHORIZE_EXAMPLE: Final = (
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=example.apps.googleusercontent.com"
    "&redirect_uri=https%3A%2F%2Fkuinn.in%2Fapi%2Fv1%2Fauth%2Fgoogle%2Fcallback"
    "&response_type=code&scope=openid+email+profile&state=st_example&nonce=nn_example"
    "&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256"
    "&prompt=select_account"
)
GOOGLE_CALLBACK_LOCATION: Final = "https://kuinn.in/auth/google?handle=gh_example_opaque_value"
ISSUE: Final = "0b1c2d3e-0000-4000-8000-000000001001"
TASK: Final = "0b1c2d3e-0000-4000-8000-000000001101"
AUDIT_EVENT: Final = "0b1c2d3e-0000-4000-8000-000000001201"
SUBMISSION: Final = "0b1c2d3e-0000-4000-8000-000000001301"
LISTING_ITEM: Final = "0b1c2d3e-0000-4000-8000-000000001401"
SIGNUP: Final = "0b1c2d3e-0000-4000-8000-000000001501"
APPLICANT: Final = "0b1c2d3e-0000-4000-8000-000000001502"

_MEMBERSHIPS: Final = [{"tenant_id": TENANT, "tenant_name": "Fabrica", "role": "owner"}]
_TOKENS: Final = {
    "access_token": "eyJhbGciOiJIUzI1NiJ9.example.signature",
    "refresh_token": "rt_example_opaque_value",
    "token_type": "bearer",
    "expires_in": 1800,
    "memberships": _MEMBERSHIPS,
}

_SIGNED_IN: Final = {
    "status": "signed_in",
    **_TOKENS,
    "challenge_token": None,
    "challenge_expires_in": None,
    "mfa_grace_until": None,
}
_RECOVERY_CODES: Final = [f"{a}{a}{a}{a}-efgh-ijkl-mnop-qrst-uvwx-yz" for a in "abcdefghjk"]

_MEMBER: Final = {
    "user_id": USER,
    "email": "owner@fabrica.example",
    "display_name": "Asha",
    "role": "owner",
    "is_active": True,
}


def _product(pid: str, sku: str, title: str) -> dict[str, Any]:
    return {
        "id": pid,
        "sku": sku,
        "title": title,
        "status": "draft",
        "category": "ethnic_wear_kurta_sets",
        "attributes": {"color": "indigo", "material": "organic cotton", "size": "M"},
        "images": [f"t/{TENANT}/img/{pid}/main.jpg"],
        "parent_product_id": None,
        "created_at": T0,
        "updated_at": T0,
    }


def _page(data: list[Any], limit: int = 50) -> dict[str, Any]:
    return {"data": data, "page": {"next_cursor": None, "has_more": False, "limit": limit}}


_JOB_ACCEPTED: Final = {
    "job_id": JOB,
    "listing_id": None,
    "status": "queued",
    "estimated_duration_seconds": 30,
    "poll_url": f"/api/v1/jobs/{JOB}",
}
_JOB_ENVELOPE: Final = {
    "schema_version": "2.0.0",
    "job_id": JOB,
    "kind": "listing_generate",
    "status": "running",
    "stage": "content_agent",
    "progress_pct": 40,
    "attempt": 1,
    "max_attempts": 3,
    "listing_id": LISTING,
    "result_url": None,
    "error": None,
    "request_id": REQUEST_ID,
    "queued_at": T0,
    "started_at": T1,
    "finished_at": None,
}
_ACCOUNT: Final = {
    "id": ACCOUNT,
    "marketplace": "amazon",
    "seller_id": "A1EXAMPLESELLER",
    "region": "eu",
    "status": "active",
    "needs_reconnect_since": None,
    "expires_at": None,
    "created_at": T0,
}

_LISTING: Final = {
    "id": LISTING,
    "product_id": PRODUCT_1,
    "marketplace": "amazon",
    "status": "needs_review",
    "status_reason": None,
    "marketplace_state": "not_submitted",
    "category": "ethnic_wear_kurta_sets",
    "current_version_id": VERSION,
    "approved_version_id": None,
    "live_url": None,
    "status_changed_at": T1,
    "created_at": T0,
    "updated_at": T1,
}
_AUDIT_EVENT: Final = {
    "id": AUDIT_EVENT,
    "action": "listing.approved",
    "actor_type": "user",
    "actor_user_id": USER_2,
    "entity_type": "listing",
    "entity_id": LISTING,
    "before": {"status": "needs_review"},
    "after": {"status": "approved"},
    "reason": None,
    "request_id": REQUEST_ID,
    "job_id": None,
    "occurred_at": T1,
}
_SUBMISSION: Final = {
    "id": SUBMISSION,
    "listing_version_id": VERSION,
    "attempt": 1,
    "operation": "create",
    "field_paths": None,
    "outcome": "accepted",
    "error_code": None,
    "retry_justification": None,
    "requested_by_user_id": USER,
    "items": [
        {
            "listing_item_id": LISTING_ITEM,
            "seller_sku": "KURTA-001",
            "marketplace_state": "processing",
            "fields": [
                {"field": "content.description", "status": "succeeded", "error": None},
                {"field": "content.title", "status": "succeeded", "error": None},
            ],
        }
    ],
    "error": None,
    "sent_at": T0,
    "responded_at": T1,
    "resolved_at": None,
    "created_at": T0,
}
_ISSUE: Final = {
    "id": ISSUE,
    "listing_id": LISTING,
    "marketplace": "amazon",
    "listing_item_id": None,
    "listing_version_id": VERSION,
    "source": "rules_engine",
    "field_path": "content.title",
    "error_code": "VALUE_NOT_ALLOWED",
    "error_class": "content_error",
    "retry_class": "user_action",
    "severity": "warning",
    "auto_fixable": False,
    "fix_action": None,
    "marketplace_code": None,
    "raw_message": None,
    "message": "The title uses a word the brand avoids.",
    "plain_message": None,
    "suggestion": "Remove the word.",
    "value_submitted": None,
    "occurrences": 1,
    "first_seen_at": T0,
    "last_seen_at": T0,
    "status": "open",
    "resolution": None,
    "action_taken": None,
    "resolved_at": None,
    "resolved_by_user_id": None,
    "human_task_id": None,
    "recurrence_of_id": None,
    "created_at": T0,
    "updated_at": T0,
}
_TASK: Final[dict[str, Any]] = {
    "id": TASK,
    "kind": "reconnect_account",
    "product_id": None,
    "colourway_id": None,
    "target_path": "account.oauth",
    "marketplace_account_id": ACCOUNT,
    "image_id": None,
    "question": "Reconnect the Amazon account.",
    "reason": "The marketplace refused the stored token.",
    "expected_answer": {"kind": "reconnect"},
    "suggestions": [],
    "status": "open",
    "answered_by_user_id": None,
    "answered_at": None,
    "resume_from": "publish",
    "applied_at": None,
    "resume_batch_id": None,
    "cancelled_reason": None,
    "created_at": T0,
    "updated_at": T0,
}
_CONTENT: Final = {
    "schema_version": "2.0.0",
    "listing_id": LISTING,
    "product_id": PRODUCT_1,
    "marketplace": "amazon",
    "category": "ethnic_wear_kurta_sets",
    "status": "needs_review",
    "version": 1,
    "locale": "en-IN",
    "content": {
        "title": "Fabrica Organic Cotton Kurta Set for Women | Indigo",
        "bullets": ["100% organic cotton, GOTS certified", "Machine washable at 30 degrees C"],
        "description": "A relaxed organic cotton kurta set in indigo.",
    },
    "keywords": {"search_terms": ["organic cotton kurta"], "hidden_terms": ["indigo kurta"]},
    "attributes": {"color": "indigo", "material": "organic cotton", "size": "M"},
    "images": [{"image_id": IMAGE, "slot": "main", "position": 0}],
    "confidence_score": 0.91,
    "generated_by_agent_run_id": None,
    "created_at": T0,
}


def _report(passed: bool) -> dict[str, Any]:
    errors = (
        []
        if passed
        else [
            {
                "field": "content.title",
                "error_code": "FIELD_TOO_LONG",
                "severity": "error",
                "message": "Title is 221 chars; Amazon limit is 200",
                "suggestion": "Shorten to <= 200 chars",
                "source": "rules_engine",
                "retryable": True,
                "marketplace": "amazon",
                "retry_class": "ai_correct",
                "error_class": "content_error",
            }
        ]
    )
    return {
        "schema_version": "2.0.0",
        "status": "passed" if passed else "failed",
        "error_code": None if passed else "VALIDATION_FAILED",
        "retryable": not passed,
        "attempts_used": 1,
        "max_attempts": 3,
        "errors": errors,
        "request_id": REQUEST_ID,
        "listing_id": LISTING,
    }


def _validation_result(passed: bool) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "validation_run_id": None,
        "tenant_id": TENANT,
        "listing_id": LISTING,
        "listing_version_id": VERSION,
        "marketplace": "amazon",
        "source": "rules_engine",
        "passed": passed,
        "attempt": 1,
        "rules_version": 3,
        "duration_ms": 12,
        "report": _report(passed),
        "created_at": T1,
    }


_VALIDATION_FAILED_SUMMARY: Final = {
    "schema_version": "2.0.0",
    "listing_id": LISTING,
    "listing_version_id": VERSION,
    "passed": False,
    "stages": [_validation_result(False)],
    "blocking_count": 1,
    "warning_count": 0,
    "request_id": REQUEST_ID,
}
_BATCH_ITEMS: Final = [
    {
        "listing_id": LISTING,
        "http_status": 200,
        "listing_status": "approved",
        "job_id": None,
        "error": None,
    }
]
_SUMMARY_COUNTS: Final = {
    "listings_generated": 120,
    "approvals": 82,
    "rejections": 18,
    "validation_first_pass_passed": 108,
    "validation_first_pass_total": 120,
    "publish_succeeded": 77,
    "publish_failed": 3,
}
_SUMMARY: Final = {
    "from": "2026-09-01",
    "to": "2026-09-30",
    "listings_generated": 120,
    "approval_rate": 0.82,
    "validation_pass_rate": 0.9,
    "publish_success_rate": 0.9625,
    "counts": _SUMMARY_COUNTS,
    "by_marketplace": [
        {
            "marketplace": "amazon",
            "listings_generated": 120,
            "approval_rate": 0.82,
            "validation_pass_rate": 0.9,
            "publish_success_rate": 0.9625,
            "counts": _SUMMARY_COUNTS,
        }
    ],
}

_SIGNUP_REQUEST: Final = {
    "id": SIGNUP,
    "user_id": APPLICANT,
    "email": "founder@kalakar.example",
    "brand_name": "Kalakar Weaves",
    "owner_name": "Meera Iyer",
    "phone": "+91 98765 43210",
    "website": "https://kalakar.example",
    "status": "pending",
    "reason": None,
    "tenant_id": None,
    "created_at": T0,
    "decided_at": None,
    "verified_via": None,
}
_MFA_REQUIRED: Final = {
    "status": "mfa_required",
    "access_token": None,
    "refresh_token": None,
    "token_type": None,
    "expires_in": None,
    "memberships": None,
    "challenge_token": "ch_example_opaque_value",
    "challenge_expires_in": 300,
    "mfa_grace_until": None,
}
_GOOGLE_SIGNUP_STATUS: Final = {
    "request_id": SIGNUP,
    "brand_name": "Acme Naturals",
    "status": "pending",
    "reason": None,
    "tenant_id": None,
    "created_at": T0,
    "decided_at": None,
}

SUCCESS_EXAMPLES: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "auth_login": {
            "status": "mfa_required",
            "access_token": None,
            "refresh_token": None,
            "token_type": None,
            "expires_in": None,
            "memberships": None,
            "challenge_token": "ch_example_opaque_value",
            "challenge_expires_in": 300,
            "mfa_grace_until": None,
        },
        "auth_login_mfa": _SIGNED_IN,
        "auth_login_mfa_enroll": {
            "otpauth_uri": "otpauth://totp/Kuinn:owner%40fabrica.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Kuinn&algorithm=SHA1&digits=6&period=30",
            "secret_b32": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        },
        "auth_login_mfa_confirm": {"recovery_codes": _RECOVERY_CODES, "result": _SIGNED_IN},
        "auth_password_expired": _SIGNED_IN,
        "auth_mfa_totp_enroll": {
            "otpauth_uri": "otpauth://totp/Kuinn:owner%40fabrica.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Kuinn&algorithm=SHA1&digits=6&period=30",
            "secret_b32": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        },
        "auth_mfa_totp_confirm": {"recovery_codes": _RECOVERY_CODES},
        "auth_security": {
            "mfa_enrolled": True,
            "recovery_codes_left": 8,
            "enforcement": "all",
            "mfa_grace_until": None,
            "password_changed_at": T0,
            "password_expires_at": "2027-10-05T09:30:00Z",
            "has_password": True,
            "google_linked": False,
        },
        "auth_mfa_recovery_codes": {"recovery_codes": _RECOVERY_CODES},
        "auth_mfa_step_up": {
            "access_token": "eyJhbGciOiJIUzI1NiJ9.example.signature",
            "token_type": "bearer",
            "expires_in": 1800,
        },
        "auth_password_change": None,
        "auth_google_start": None,
        "auth_google_callback": None,
        "auth_google_complete": {
            "outcome": "needs_details",
            "login": None,
            "signup": None,
            "details": {
                "email": "asha@acmenaturals.example",
                "email_verified": True,
                "owner_name": "Asha Rao",
                "given_name": "Asha",
                "family_name": "Rao",
                "suggested_brand_name": "Acmenaturals",
                "details_handle": "gd_example_opaque_value",
                "details_expires_in": 900,
            },
            "link": None,
        },
        "auth_google_link": _MFA_REQUIRED,
        "auth_google_unlink": None,
        "auth_refresh": _TOKENS,
        "auth_logout": None,
        "auth_me": {
            "user_id": USER,
            "email": "owner@fabrica.example",
            "display_name": "Asha",
            "tenant_id": TENANT,
            "role": "owner",
            "memberships": _MEMBERSHIPS,
            "is_platform_admin": False,
        },
        "auth_switch_tenant": _TOKENS,
        "members_list": _page(
            [
                _MEMBER,
                {
                    **_MEMBER,
                    "user_id": USER_2,
                    "email": "ravi@fabrica.example",
                    "display_name": "Ravi",
                    "role": "reviewer",
                },
            ]
        ),
        "members_patch": {
            **_MEMBER,
            "user_id": USER_2,
            "email": "ravi@fabrica.example",
            "display_name": "Ravi",
            "role": "operator",
        },
        "tenants_me": {
            "id": TENANT,
            "name": "Fabrica",
            "slug": "fabrica",
            "tone": "Warm, plain and factual.",
            "guidelines": ["Never claim 'best'."],
            "glossary": {"kurta set": "kurta and palazzo"},
            "status": "active",
        },
        "tenants_me_update": {
            "id": TENANT,
            "name": "Fabrica",
            "slug": "fabrica",
            "tone": "Warm, plain and factual.",
            "guidelines": ["Never claim 'best'.", "Name the fabric first."],
            "glossary": {"kurta outfit": "kurta set"},
            "status": "active",
        },
        "brand_documents_upload": _JOB_ACCEPTED,
        "brand_documents_list": _page(
            [
                {
                    "id": DOCUMENT,
                    "filename": "brand-voice.md",
                    "status": "ready",
                    "chunk_count": 42,
                    "error": None,
                    "created_at": T0,
                }
            ]
        ),
        "brand_documents_delete": None,
        "products_create": _product(PRODUCT_1, "FAB-KURTA-001", "Organic Cotton Kurta Set"),
        "products_list": _page(
            [
                _product(PRODUCT_1, "FAB-KURTA-001", "Organic Cotton Kurta Set"),
                _product(PRODUCT_2, "FAB-KURTA-002", "Handloom Cotton Kurta"),
            ]
        ),
        "products_get": _product(PRODUCT_1, "FAB-KURTA-001", "Organic Cotton Kurta Set"),
        "products_bulk": {
            "import_id": IMPORT,
            "job_id": JOB,
            "status": "queued",
            "poll_url": f"/api/v1/imports/{IMPORT}",
        },
        "imports_get": {
            "schema_version": "2.0.0",
            "import_id": IMPORT,
            "total_rows": 3,
            "imported": 2,
            "failed": 1,
            "rows": [],
            "error": None,
        },
        "imports_rows": _page(
            [
                {
                    "row_number": 3,
                    "sku": "FAB-KURTA-003",
                    "status": "failed",
                    "product_id": None,
                    "errors": [
                        {
                            "field": "file.row",
                            "error_code": "REQUIRED_FIELD_MISSING",
                            "severity": "error",
                            "message": "title is required",
                            "source": "schema",
                            "retryable": False,
                        }
                    ],
                }
            ]
        ),
        "imports_rows_retry": _JOB_ACCEPTED,
        "listings_generate": {**_JOB_ACCEPTED, "listing_id": LISTING},
        "listings_validate": _VALIDATION_FAILED_SUMMARY,
        "listings_approve": {"batch_id": BATCH, "items": _BATCH_ITEMS},
        "listings_publish": {
            "batch_id": BATCH,
            "items": [
                {
                    **_BATCH_ITEMS[0],
                    "http_status": 202,
                    "listing_status": "publishing",
                    "job_id": JOB,
                }
            ],
            "poll_url": f"/api/v1/jobs/{JOB}",
        },
        "listings_list": _page([_LISTING]),
        "listings_get": {**_LISTING, "content": _CONTENT, "validation": _VALIDATION_FAILED_SUMMARY},
        "listings_preview": {
            "schema_version": "2.0.0",
            "listing_id": LISTING,
            "version": 1,
            "marketplace": "amazon",
            "product_type": "SHIRT",
            "items": [
                {
                    "product_id": PRODUCT_1,
                    "seller_sku": "KURTA-001",
                    "item_role": "standalone",
                    "payload": {
                        "productType": "SHIRT",
                        "requirements": "LISTING_PRODUCT_ONLY",
                        "attributes": {
                            "item_name": [
                                {
                                    "value": "Indigo Organic Cotton Kurta",
                                    "language_tag": "en_IN",
                                    "marketplace_id": "A21TJRUUN4KGV",
                                }
                            ],
                            "main_product_image_locator": [
                                {
                                    "media_location": f"image:{IMAGE}",
                                    "marketplace_id": "A21TJRUUN4KGV",
                                }
                            ],
                        },
                    },
                }
            ],
            "report": {**_report(True), "request_id": None},
        },
        "listings_status": {
            "listing_id": LISTING,
            "status": "needs_review",
            "status_reason": None,
            "marketplace_state": "not_submitted",
            "status_changed_at": T1,
        },
        "listings_content_patch": {**_LISTING, "content": _CONTENT, "validation": None},
        "listings_regenerate": {**_JOB_ACCEPTED, "listing_id": LISTING},
        "listings_retry_fields": {**_JOB_ACCEPTED, "listing_id": LISTING},
        "listings_submissions": _page([_SUBMISSION]),
        "listings_refresh_status": {**_JOB_ACCEPTED, "listing_id": LISTING},
        "marketplaces_connect": {
            "authorize_url": "https://sellercentral.amazon.in/apps/authorize/consent?state=st_ex"
        },
        "marketplaces_callback": {"location": CALLBACK_LOCATION},
        "marketplaces_connect_confirm": _ACCOUNT,
        "marketplaces_accounts_revoke": {
            **_ACCOUNT,
            "status": "revoked",
            "needs_reconnect_since": None,
            "expires_at": None,
        },
        "marketplaces_accounts": _page([_ACCOUNT]),
        "images_upload_url": {
            "upload_url": "http://localhost:9000/marketplace-images/t/example?X-Amz-Signature=ex",
            "storage_key": f"t/{TENANT}/img/{PRODUCT_1}/main.jpg",
            "expires_at": T1,
            "headers": {"Content-Type": "image/jpeg"},
        },
        "images_upload": {
            "image_id": IMAGE,
            "job_id": JOB,
            "status": "queued",
            "poll_url": f"/api/v1/jobs/{JOB}",
        },
        "images_report": {
            "image_id": IMAGE,
            "compliance_status": "passed",
            "reports": {
                "amazon": {
                    "schema_version": "2.0.0",
                    "image_id": IMAGE,
                    "marketplace": "amazon",
                    "slot": "main",
                    "pass": True,
                    "checks": [{"check": "resolution", "method": "deterministic", "passed": True}],
                    "issues": [],
                    "confidence": None,
                    "model_used": None,
                    "agent_run_id": None,
                    "processed_at": T1,
                },
                "flipkart": None,
            },
        },
        "rules_list": _page(
            [
                {
                    "id": RULE,
                    "marketplace": "amazon",
                    "category": None,
                    "field": "content.title",
                    "rule_type": "max_length",
                    "severity": "error",
                    "version": 1,
                    "enabled": True,
                    "scope": "platform",
                }
            ]
        ),
        "rules_dry_run": _validation_result(False),
        "jobs_get": _JOB_ENVELOPE,
        "jobs_cancel": {**_JOB_ENVELOPE, "status": "cancelled", "stage": None, "finished_at": T1},
        "listings_history": _page([_AUDIT_EVENT]),
        "issues_list": _page([_ISSUE]),
        "issues_dismiss": {
            **_ISSUE,
            "status": "dismissed",
            "resolution": "accepted_warning",
            "resolved_at": T1,
            "resolved_by_user_id": USER_2,
            "updated_at": T1,
        },
        "tasks_list": _page([_TASK]),
        "analytics_get": _SUMMARY,
        "analytics_summary": _SUMMARY,
        "signup_create": {"request_id": SIGNUP, "status": "pending"},
        "signup_status": {
            "request_id": SIGNUP,
            "brand_name": "Kalakar Weaves",
            "status": "pending",
            "reason": None,
            "tenant_id": None,
            "created_at": T0,
            "decided_at": None,
        },
        "signup_google": _GOOGLE_SIGNUP_STATUS,
        "admin_signup_requests_list": _page([_SIGNUP_REQUEST]),
        "admin_signup_requests_approve": {
            **_SIGNUP_REQUEST,
            "status": "approved",
            "tenant_id": TENANT,
            "decided_at": T1,
        },
        "admin_signup_requests_reject": {
            **_SIGNUP_REQUEST,
            "status": "rejected",
            "reason": "We could not verify the brand; write to support with your GSTIN.",
            "decided_at": T1,
        },
    }
)
