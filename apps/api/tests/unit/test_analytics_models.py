"""S23 T2: `AnalyticsSummary.counts` and `by_marketplace` (additive MINOR under the open 1.1.0)."""

from __future__ import annotations

import math
from typing import Any

import pytest
from app.api.v1.examples import SUCCESS_EXAMPLES
from app.api.v1.schemas.analytics import AnalyticsCounts, AnalyticsSummary, MarketplaceBreakdown
from pydantic import ValidationError

COUNTS: dict[str, int] = {
    "listings_generated": 3,
    "approvals": 2,
    "rejections": 1,
    "validation_first_pass_passed": 2,
    "validation_first_pass_total": 3,
    "publish_succeeded": 1,
    "publish_failed": 1,
}
S3_EXAMPLE: dict[str, Any] = {
    "from": "2026-09-01",
    "to": "2026-09-30",
    "listings_generated": 120,
    "approval_rate": 0.82,
    "validation_pass_rate": 0.9,
    "publish_success_rate": 0.97,
}


@pytest.mark.parametrize("name", sorted(COUNTS))
def test_counts_non_negative(name: str) -> None:
    AnalyticsCounts.model_validate(COUNTS)
    with pytest.raises(ValidationError):
        AnalyticsCounts.model_validate({**COUNTS, name: -1})


@pytest.mark.parametrize("bad", [-0.0001, 1.0001, math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("field", ["approval_rate", "validation_pass_rate", "publish_success_rate"])
def test_rates_between_0_and_1_or_null(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        AnalyticsSummary.model_validate({**S3_EXAMPLE, field: bad})
    with pytest.raises(ValidationError):
        MarketplaceBreakdown.model_validate(
            {"marketplace": "amazon", "listings_generated": 0, "counts": COUNTS, field: bad}
        )
    for ok in (None, 0.0, 1.0, 0.3333):
        AnalyticsSummary.model_validate({**S3_EXAMPLE, field: ok})


def test_by_marketplace_uses_marketplace_enum() -> None:
    row = {"marketplace": "amazon", "listings_generated": 3, "counts": COUNTS}
    ok = AnalyticsSummary.model_validate({**S3_EXAMPLE, "counts": COUNTS, "by_marketplace": [row]})
    assert ok.by_marketplace is not None and ok.by_marketplace[0].marketplace == "amazon"
    with pytest.raises(ValidationError):
        AnalyticsSummary.model_validate(
            {**S3_EXAMPLE, "by_marketplace": [{**row, "marketplace": "ebay"}]}
        )
    with pytest.raises(ValidationError):
        AnalyticsSummary.model_validate({**S3_EXAMPLE, "by_marketplace": [row, row, row]})


def test_summary_without_new_fields_still_valid() -> None:
    summary = AnalyticsSummary.model_validate(S3_EXAMPLE)
    assert summary.counts is None and summary.by_marketplace is None


@pytest.mark.parametrize("op_id", ["analytics_get", "analytics_summary"])
def test_success_example_carries_counts_and_breakdown(op_id: str) -> None:
    example = SUCCESS_EXAMPLES[op_id]
    summary = AnalyticsSummary.model_validate(example)
    assert summary.counts is not None and summary.by_marketplace
