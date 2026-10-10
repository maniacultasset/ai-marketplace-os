"""S21 publish_ledger routing, the exact Decision fields (S26 T23 gate-fix; AC 11): the rows and
item states written, the default report of a result without one, the task after an exhausted
attempt and the whole-call error item. test_publish_ledger.py covers the routing table."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.services.publish_ledger import EXHAUSTED, FieldRow, ItemState, route_error, route_result
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, ItemMarketplaceState, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.publish import ListingResult
from tests.unit.test_publish_ledger import _err

LISTING = uuid.UUID(int=7)
PRODUCT = uuid.UUID(int=8)


def _listing_result(
    status: str,
    *,
    fields: list[dict[str, Any]] | None = None,
    state: str = "processing",
    error: ErrorReport | None = None,
) -> ListingResult:
    return ListingResult.model_validate(
        {
            "listing_id": LISTING,
            "marketplace": "amazon",
            "status": status,
            "idempotency_key": "k",
            "external_id": "EXT-1",
            "submission_ref": "sub-9",
            "items": [
                {
                    "product_id": PRODUCT,
                    "seller_sku": "SKU-1",
                    "marketplace_state": state,
                    "external_id": "ASIN1",
                    "fields": fields or [],
                }
            ],
            "error": error,
        }
    )


def test_rows_keep_the_first_of_a_repeated_field_and_go_on() -> None:
    fields = [
        {"field": "content.title", "status": "succeeded"},
        {"field": "content.title", "status": "failed"},
        {"field": "content.bullets", "status": "skipped"},
    ]
    d = route_result(_listing_result("published", fields=fields, state="inactive"), attempt=1)
    assert d.field_rows == (
        FieldRow("SKU-1", "content.title", "succeeded", None),
        FieldRow("SKU-1", "content.bullets", "skipped", None),
    )
    assert d.items == (ItemState("SKU-1", ItemMarketplaceState.INACTIVE, "ASIN1"),)
    assert (d.submission_ref, d.external_id) == ("sub-9", "EXT-1")


@pytest.mark.parametrize(
    ("status", "report_status", "code", "outcome"),
    [
        ("failed", "failed", ErrorCode.PUBLISH_FAILED, "rejected"),
        ("partially_published", "partial", ErrorCode.API_REJECTED_PARTIAL, "partially_accepted"),
    ],
)
def test_result_without_a_report_gets_the_default(
    status: str, report_status: str, code: ErrorCode, outcome: str
) -> None:
    d = route_result(_listing_result(status, state="rejected"), attempt=1)
    assert d.report is not None
    assert (d.report.status, d.report.error_code, d.error_code) == (report_status, code, code)
    assert d.outcome == outcome
    assert d.open_task is None
    assert d.items == (ItemState("SKU-1", ItemMarketplaceState.REJECTED, "ASIN1"),)
    assert (d.submission_ref, d.external_id) == ("sub-9", "EXT-1")


def _report(*errors: ErrorItem) -> ErrorReport:
    return ErrorReport(status="failed", error_code=None, retryable=False, errors=list(errors))


def test_exhausted_unknown_code_still_opens_the_review_task() -> None:
    unknown = _err(ErrorCode.MARKETPLACE_UNKNOWN_ERROR, klass=ErrorClass.UNKNOWN)
    d = route_result(_listing_result("failed", error=_report(unknown)), attempt=3)
    assert (d.listing_event, d.listing_to, d.reason) == EXHAUSTED
    assert d.open_task == "marketplace_error_review"


def test_exhausted_known_code_opens_no_task() -> None:
    d = route_result(_listing_result("failed", error=_report(_err())), attempt=3)
    assert (d.listing_event, d.listing_to, d.reason) == EXHAUSTED
    assert d.open_task is None


@pytest.mark.parametrize(
    ("kind", "code", "retryable", "message"),
    [
        (
            "auth_expired",
            ErrorCode.AUTH_EXPIRED,
            False,
            "The marketplace refused the connection; reconnect the account.",
        ),
        (
            "rate_limited",
            ErrorCode.RATE_LIMITED,
            True,
            "The marketplace throttled the request; it is retried.",
        ),
        (
            "permission_denied",
            ErrorCode.ACCOUNT_PERMISSION_DENIED,
            False,
            "The seller account lacks a permission this needs.",
        ),
    ],
)
def test_whole_call_error_gets_its_one_listing_item(
    kind: Any, code: ErrorCode, retryable: bool, message: str
) -> None:
    from mkos_schemas.codes import ERROR_CODE_META

    d = route_error(kind, _report(), attempt=1)
    assert d.report is not None
    assert (d.report.error_code, d.report.retryable, d.error_code) == (code, retryable, code)
    meta = ERROR_CODE_META[code]
    assert d.report.errors == [
        ErrorItem(
            field="listing",
            error_code=code,
            message=message,
            source=ErrorSource.AMAZON_ADAPTER,
            retryable=retryable,
            retry_class=meta.retry_class,
            error_class=meta.error_class,
        )
    ]


def test_whole_call_error_keeps_the_adapter_items() -> None:
    item = _err(ErrorCode.AUTH_EXPIRED, field="listing", retry=RetryClass.USER_ACTION)
    d = route_error("auth_expired", _report(item), attempt=1)
    assert d.report is not None and d.report.errors == [item]


def test_exhausted_error_is_failed_with_the_exhausted_move() -> None:
    d = route_error("auth_expired", _report(), attempt=3)
    assert (d.outcome, d.job_status, d.error_code) == ("error", "failed", ErrorCode.AUTH_EXPIRED)
    assert (d.listing_event, d.listing_to, d.reason) == EXHAUSTED
    assert d.open_task is None


@pytest.mark.parametrize("kind", ["ambiguous", "unavailable"])
def test_ambiguous_keeps_the_report_as_given(kind: Any) -> None:
    report = _report(_err())
    d = route_error(kind, report, attempt=1)
    assert (d.outcome, d.job_status, d.report) == ("pending", "retry", report)
