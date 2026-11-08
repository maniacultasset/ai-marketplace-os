"""SI11 T19: the error catalogue's pure staff view (lane, suggested map row)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from app.services.resolution.catalog import catalog_line, suggested_row

NOW = datetime(2026, 11, 8, tzinfo=UTC)


@dataclass
class Stat:
    marketplace: str = "amazon"
    marketplace_code: str | None = "77777"
    error_code: str = "MARKETPLACE_UNKNOWN_ERROR"
    error_class: str = "unknown"
    mapped: bool = False
    issues: int = 3
    occurrences: int = 5
    tenants: int = 2
    first_seen: datetime = NOW
    last_seen: datetime = NOW
    preview: bool = True
    resolutions: dict[str, int] = field(default_factory=lambda: {"open": 2, "regenerated": 1})
    recurrence_rate: Decimal = Decimal("0.333")


def test_an_unmapped_code_gets_a_reviewable_row() -> None:
    row = suggested_row(Stat())
    assert row is not None
    assert (row["raw_value"], row["error_code"], row["error_class"]) == (
        "77777",
        "MARKETPLACE_UNKNOWN_ERROR",
        "unknown",
    )
    assert row["fix_action"] is None and row["policy_ref"] == {
        "source": "observed",
        "observed": True,
    }


def test_a_mapped_code_or_one_with_no_raw_code_gets_none() -> None:
    assert suggested_row(Stat(mapped=True)) is None
    assert suggested_row(Stat(marketplace_code=None)) is None


def test_the_line_names_the_lane_and_no_text() -> None:
    line = catalog_line(Stat(error_code="VALUE_NOT_ALLOWED", error_class="account_permission"))
    assert line["lane"] == "marketplace"
    assert set(line) >= {"tenants", "issues", "resolutions", "suggested_row"}
    assert "message" not in line and "tenant_id" not in line
