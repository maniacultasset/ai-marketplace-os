"""SI10 T3: the pure AI credit rules (AC5, AC8). One predicate for the API precheck and the worker
claim, the refusal classifier the worker reads after a run, and the admin state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from app.repositories.budget import BudgetState
from app.services.ai_credit import (
    CREDIT_ALERT_PCTS,
    CREDIT_FLOOR_USD,
    FRESH_FOR,
    REASON_CODE,
    CreditRow,
    admission_refusal,
    classify_refusal,
    credit_refusal,
    state_of,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
AMOUNT = Decimal("0.02")
CAP = Decimal("1")


def _budget(used: str, cap: Decimal = CAP) -> BudgetState:
    return BudgetState(spent=Decimal(used), reserved=Decimal(0), cap=cap)


def _fresh(remaining: str | None, *, age: timedelta = timedelta(minutes=1)) -> CreditRow:
    return CreditRow(
        remaining_usd=None if remaining is None else Decimal(remaining), checked_at=NOW - age
    )


OK = _budget("0")
EVERYONE_OK = _budget("0", Decimal("3"))


def test_constants() -> None:
    assert Decimal("0.50") == CREDIT_FLOOR_USD  # the 0021 auto-clear function hard-codes it
    assert timedelta(minutes=15) == FRESH_FOR
    assert CREDIT_ALERT_PCTS == (50, 80, 95)
    assert REASON_CODE == {
        "credit_exhausted": ErrorCode.AI_CREDIT_EXHAUSTED,
        "key_invalid": ErrorCode.AI_PROVIDER_KEY_INVALID,
    }


# ---- admission_refusal (rule 1 credit, rule 2 tenant, rule 3 global) ----


@pytest.mark.parametrize(
    ("row", "own", "everyone", "want"),
    [
        (
            CreditRow(paused_reason="credit_exhausted", paused_at=NOW),
            OK,
            EVERYONE_OK,
            "credit_exhausted",
        ),
        (CreditRow(paused_reason="key_invalid", paused_at=NOW), OK, EVERYONE_OK, "key_invalid"),
        (_fresh("0.49"), OK, EVERYONE_OK, "credit_exhausted"),
        (_fresh("0.50"), OK, EVERYONE_OK, None),  # at the floor admits
        (_fresh("0.10", age=timedelta(minutes=16)), OK, EVERYONE_OK, None),  # stale admits
        (CreditRow(), OK, EVERYONE_OK, None),  # never checked admits
        (_fresh(None), OK, EVERYONE_OK, None),  # unlimited admits
        (CreditRow(), _budget("0.99"), EVERYONE_OK, "tenant"),  # 0.99 + 0.02 > 1
        (CreditRow(), _budget("0.97"), EVERYONE_OK, None),  # 0.97 + 0.02 <= 1
        (CreditRow(), OK, _budget("3", Decimal("3")), "global"),
        (CreditRow(), _budget("1"), _budget("3", Decimal("3")), "tenant"),  # tenant first
        (_fresh("0.10"), _budget("1"), EVERYONE_OK, "credit_exhausted"),  # credit first
        (CreditRow(), OK, None, None),  # everyone not read yet (reserve reads it under its locks)
    ],
)
def test_admission_refusal_table(
    row: CreditRow, own: BudgetState, everyone: BudgetState | None, want: str | None
) -> None:
    assert admission_refusal(row, own, everyone, amount=AMOUNT, now=NOW) == want


def test_admission_refusal_naive_now_raises() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        admission_refusal(CreditRow(), OK, EVERYONE_OK, amount=AMOUNT, now=NOW.replace(tzinfo=None))


def test_credit_refusal_reports_the_pause_first() -> None:
    row = CreditRow(
        paused_reason="key_invalid", paused_at=NOW, remaining_usd=Decimal("0.1"), checked_at=NOW
    )
    assert credit_refusal(row, NOW) == "key_invalid"


DECIMALS = st.decimals(min_value=Decimal(0), max_value=Decimal(20), places=6, allow_nan=False)


@settings(max_examples=200, deadline=None)
@given(remaining=DECIMALS, own_used=DECIMALS, own_cap=DECIMALS, all_used=DECIMALS, all_cap=DECIMALS)
def test_admission_matches_the_rule_order(
    remaining: Decimal, own_used: Decimal, own_cap: Decimal, all_used: Decimal, all_cap: Decimal
) -> None:
    row = CreditRow(remaining_usd=remaining, checked_at=NOW)
    own = BudgetState(spent=own_used, reserved=Decimal(0), cap=own_cap)
    everyone = BudgetState(spent=all_used, reserved=Decimal(0), cap=all_cap)
    if remaining < Decimal("0.50"):
        want: str | None = "credit_exhausted"
    elif own_used + AMOUNT > own_cap:
        want = "tenant"
    elif all_used + AMOUNT > all_cap:
        want = "global"
    else:
        want = None
    assert admission_refusal(row, own, everyone, amount=AMOUNT, now=NOW) == want


# ---- classify_refusal ----


def _record(status: str, code: str | None) -> AgentRunRecord:
    report = None
    if code is not None:
        error_code = ErrorCode(code)
        retry_class = ERROR_CODE_META[error_code].retry_class
        retryable = retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT)
        report = ErrorReport(
            status="failed",
            error_code=error_code,
            retryable=retryable,
            errors=[
                ErrorItem(
                    field="listing",
                    error_code=error_code,
                    message="the call failed",
                    source=ErrorSource.AI_MODEL,
                    retryable=retryable,
                    retry_class=retry_class,
                )
            ],
        )
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "request_id": "req-1",
        "agent_name": "content",
        "trigger": "generate",
        "status": status,
        "error_report": report,
        "started_at": NOW,
        "created_at": NOW,
    }
    return AgentRunRecord(**fields)


@pytest.mark.parametrize(
    ("records", "embed", "want"),
    [
        ([], None, None),
        ([_record("completed", "AI_CREDIT_EXHAUSTED")], None, None),  # only failed records count
        ([_record("failed", "AI_CREDIT_EXHAUSTED")], None, "credit"),
        ([_record("failed", "BUDGET_EXCEEDED")], None, "credit"),  # legacy 0.3.1 402 (decision 16)
        ([_record("failed", "AI_PROVIDER_KEY_INVALID")], None, "key_suspect"),  # 0.3.2
        ([_record("failed", "AI_CREDIT_EXHAUSTED")], "key_suspect", "credit"),  # credit wins
        ([_record("failed", "MODEL_UNAVAILABLE")], None, None),
        ([_record("failed", "INTERNAL_ERROR")], None, None),  # 0.3.0/0.3.1 401/403
        ([_record("failed", None)], None, None),
        ([], "credit", "credit"),
        ([], "key_suspect", "key_suspect"),
        ([_record("failed", "AI_PROVIDER_KEY_INVALID")], "credit", "credit"),
    ],
)
def test_classify_refusal(records: list[AgentRunRecord], embed: Any, want: str | None) -> None:
    assert classify_refusal(records, embed) == want


# ---- state_of (CP-D9) ----


@pytest.mark.parametrize(
    ("row", "want"),
    [
        (CreditRow(paused_reason="credit_exhausted", paused_at=NOW), "paused"),
        (_fresh("0.20"), "low"),
        (_fresh("0.20", age=timedelta(minutes=20)), "unknown"),
        (CreditRow(), "unknown"),
        (_fresh("5.00"), "ok"),
        (_fresh(None), "ok"),  # unlimited and fresh
    ],
)
def test_state_of(row: CreditRow, want: str) -> None:
    assert state_of(row, NOW) == want
