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
CALLBACK_LOCATION: Final = "/marketplaces/connected?pending=pnd_8f3a2c1e9b7d4f60a1c2"

_MEMBERSHIPS: Final = [{"tenant_id": TENANT, "tenant_name": "Fabrica", "role": "owner"}]
_TOKENS: Final = {
    "access_token": "eyJhbGciOiJIUzI1NiJ9.example.signature",
    "refresh_token": "rt_example_opaque_value",
    "token_type": "bearer",
    "expires_in": 1800,
    "memberships": _MEMBERSHIPS,
}

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
    "schema_version": "1.0.0",
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
_CONTENT: Final = {
    "schema_version": "1.0.0",
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
        "schema_version": "1.0.0",
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
        "schema_version": "1.0.0",
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
    "schema_version": "1.0.0",
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
_SUMMARY: Final = {
    "from": "2026-09-01",
    "to": "2026-09-30",
    "listings_generated": 120,
    "approval_rate": 0.82,
    "validation_pass_rate": 0.9,
    "publish_success_rate": 0.97,
}

SUCCESS_EXAMPLES: Final[Mapping[str, Any]] = MappingProxyType(
    {
        "auth_login": _TOKENS,
        "auth_refresh": _TOKENS,
        "auth_logout": None,
        "auth_me": {
            "user_id": USER,
            "email": "owner@fabrica.example",
            "display_name": "Asha",
            "tenant_id": TENANT,
            "role": "owner",
            "memberships": _MEMBERSHIPS,
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
        "brand_documents_upload": _JOB_ACCEPTED,
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
            "schema_version": "1.0.0",
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
            "schema_version": "1.0.0",
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
                        "requirements": "LISTING",
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
                    "schema_version": "1.0.0",
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
        "analytics_get": _SUMMARY,
        "analytics_summary": _SUMMARY,
    }
)
