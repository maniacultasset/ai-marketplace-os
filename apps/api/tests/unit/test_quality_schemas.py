"""SI14 T4: the quality summary's rates are shares in [0, 1] or null."""

from __future__ import annotations

from typing import Any

import pytest
from app.api.v1.schemas.analytics import QualitySummary
from pydantic import ValidationError


def _body(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"from": "2026-11-01", "to": "2026-11-09", "listings_generated": 0}
    raw.update(changes)
    return raw


def test_empty_rates_are_null() -> None:
    summary = QualitySummary.model_validate(_body())
    assert summary.first_pass_rate is None and summary.by_tenant is None


@pytest.mark.parametrize("field", ["first_pass_rate", "blocked_rate", "edit_rate"])
@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan")])
def test_rates_bounded(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        QualitySummary.model_validate(_body(**{field: value}))


def test_range_order() -> None:
    with pytest.raises(ValidationError):
        QualitySummary.model_validate(_body(**{"from": "2026-11-10"}))
