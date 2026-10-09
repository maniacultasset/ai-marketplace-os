"""S21 T5 (AC3, AC5, AC9; DATABASE.md :871, :880, :890, §6.4; D11): the pure outcome routing."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from app.services.publish_ledger import Decision, route_error, route_result, stored_report
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.publish import ListingResult

TRANSITIONS = Path(__file__).resolve().parents[2] / "alembic/sql/0004/01_transitions.up.sql"
LISTING = uuid.UUID(int=7)
PRODUCT = uuid.UUID(int=8)


def _edges() -> set[tuple[str, str, str]]:
    import re

    text = TRANSITIONS.read_text(encoding="utf-8")
    return {
        (m[0], m[1], m[2])
        for m in re.findall(r"\('(\w+)', '(\w+)', '(\w+)', '(?:user|worker|any)'", text)
    }


def _err(
    code: ErrorCode = ErrorCode.RATE_LIMITED,
    field: str = "content.title",
    retry: RetryClass | None = None,
    klass: ErrorClass | None = None,
) -> ErrorItem:
    retry = retry or (
        RetryClass.AUTO_RETRY if code is ErrorCode.RATE_LIMITED else RetryClass.USER_ACTION
    )
    return ErrorItem(
        field=field,
        error_code=code,
        message="x",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=retry is RetryClass.AUTO_RETRY,
        retry_class=retry,
        error_class=klass,
        raw_code="RAW",
    )


def _result(status: str, errors: list[ErrorItem] = (), state: str = "processing") -> ListingResult:  # type: ignore[assignment]
    fields: list[dict[str, Any]] = [{"field": "content.description", "status": "succeeded"}]
    fields += [{"field": e.field, "status": "failed", "error": e} for e in errors]
    report = None
    if status != "published":
        report = ErrorReport(
            status="partial" if status == "partially_published" else "failed",
            error_code=None,
            retryable=True,
            errors=errors,
        )
    return ListingResult.model_validate(
        {
            "listing_id": LISTING,
            "marketplace": "amazon",
            "status": status,
            "idempotency_key": "k",
            "items": [
                {
                    "product_id": PRODUCT,
                    "seller_sku": "SKU-1",
                    "marketplace_state": state,
                    "external_id": "ASIN1",
                    "fields": fields,
                }
            ],
            "error": report,
            "submission_ref": "sub-1",
        }
    )


def test_route_accepted() -> None:
    d = route_result(_result("published"), attempt=1)
    assert (d.outcome, d.job_status, d.listing_event, d.listing_to) == (
        "accepted",
        "succeeded",
        "submission_accepted",
        "published",
    )
    assert d.submission_ref == "sub-1"


def test_route_accepted_live_is_written_processing() -> None:
    d = route_result(_result("published", state="live"), attempt=1)
    assert [i.state.value for i in d.items] == ["processing"]


def test_route_partial() -> None:
    d = route_result(_result("partially_published", [_err()]), attempt=1)
    assert (d.outcome, d.job_status, d.listing_to, d.reason) == (
        "partially_accepted",
        "partial",
        "failed",
        "publish_partial",
    )
    assert d.report is not None and d.report.retryable
    assert d.error_code is ErrorCode.API_REJECTED_PARTIAL


def test_route_rejected() -> None:
    d = route_result(_result("failed", [_err()]), attempt=1)
    assert (d.outcome, d.job_status, d.reason) == ("rejected", "failed", "publish_failed")
    assert d.error_code is ErrorCode.PUBLISH_FAILED


def test_route_unknown_code_blocks_and_task() -> None:
    d = route_result(
        _result("partially_published", [_err(ErrorCode.MARKETPLACE_UNKNOWN_ERROR)]), attempt=1
    )
    assert (d.listing_to, d.reason, d.open_task) == (
        "blocked",
        "marketplace_action_required",
        "marketplace_error_review",
    )


def test_route_permission_class_blocks() -> None:
    item = _err(ErrorCode.ACCOUNT_PERMISSION_DENIED, klass=ErrorClass.ACCOUNT_PERMISSION)
    d = route_result(_result("failed", [item]), attempt=1)
    assert (d.reason, d.open_task) == ("marketplace_action_required", "permission_decision")


def test_route_sku_conflict_blocks() -> None:
    d = route_result(_result("failed", [_err(ErrorCode.SELLER_SKU_CONFLICT)]), attempt=2)
    assert d.reason == "marketplace_action_required"


def test_route_third_attempt_exhausted() -> None:
    d = route_result(_result("partially_published", [_err()]), attempt=3)
    assert (d.listing_event, d.listing_to, d.reason) == (
        "submission_attempts_exhausted",
        "blocked",
        "publish_attempts_exhausted",
    )


def test_partial_report_retryable_iff_all_blocking_retryable() -> None:
    mixed = route_result(
        _result(
            "partially_published",
            [_err(), _err(ErrorCode.VALUE_NOT_ALLOWED, field="attributes.color")],
        ),
        attempt=1,
    )
    assert mixed.report is not None and not mixed.report.retryable
    assert mixed.reason == "publish_partial"


def test_route_rate_limited_error_retry() -> None:
    d = route_error("rate_limited", ErrorReport(status="failed"), attempt=1)
    assert (d.outcome, d.job_status, d.error_code) == ("error", "retry", ErrorCode.RATE_LIMITED)
    assert d.report is not None and d.report.retryable and d.listing_event is None


def test_route_rate_limited_third_exhausted() -> None:
    d = route_error("rate_limited", ErrorReport(status="failed"), attempt=3)
    assert (d.job_status, d.reason) == ("failed", "publish_attempts_exhausted")


def test_route_auth_expired() -> None:
    d = route_error("auth_expired", ErrorReport(status="failed"), attempt=1)
    assert (d.outcome, d.error_code, d.reason, d.open_task) == (
        "error",
        ErrorCode.AUTH_EXPIRED,
        "marketplace_action_required",
        "link_reconnect",
    )
    assert d.report is not None and not d.report.retryable


def test_route_permission_denied() -> None:
    d = route_error("permission_denied", ErrorReport(status="failed"), attempt=2)
    assert (d.error_code, d.open_task) == (
        ErrorCode.ACCOUNT_PERMISSION_DENIED,
        "permission_decision",
    )


@pytest.mark.parametrize("kind", ["ambiguous", "unavailable"])
def test_route_ambiguous_stays_pending(kind: Any) -> None:
    d = route_error(kind, ErrorReport(status="failed"), attempt=1)
    assert (d.outcome, d.job_status, d.listing_event) == ("pending", "retry", None)


def test_field_rows_one_per_item_field() -> None:
    d = route_result(_result("partially_published", [_err()]), attempt=1)
    assert [(r.seller_sku, r.field, r.status) for r in d.field_rows] == [
        ("SKU-1", "content.description", "succeeded"),
        ("SKU-1", "content.title", "failed"),
    ]


def test_report_is_one_error_report() -> None:
    d = route_result(_result("failed", [_err()]), attempt=1)
    assert isinstance(d.report, ErrorReport)
    assert stored_report(d.report).retryable is d.report.retryable


@settings(max_examples=60, deadline=None)
@given(
    st.sampled_from(["published", "partially_published", "failed"]),
    st.integers(1, 3),
    st.lists(
        st.sampled_from(
            [
                ErrorCode.RATE_LIMITED,
                ErrorCode.VALUE_NOT_ALLOWED,
                ErrorCode.MARKETPLACE_UNKNOWN_ERROR,
                ErrorCode.SELLER_SKU_CONFLICT,
            ]
        ),
        min_size=1,
        max_size=3,
    ),
)
def test_every_result_routes(status: str, attempt: int, codes: list[ErrorCode]) -> None:
    errors = [_err(c, field=f"attributes.a{i}") for i, c in enumerate(codes)]
    d: Decision = route_result(
        _result(status, [] if status == "published" else errors), attempt=attempt
    )
    assert d.listing_event is not None and d.listing_to is not None
    assert ("publishing", d.listing_to, d.listing_event) in _edges()
