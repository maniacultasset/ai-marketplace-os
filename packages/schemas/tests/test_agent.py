"""AgentRunRecord and Trigger vocabulary."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from mkos_schemas.agent import AgentName, AgentRunRecord, Trigger

_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_TID = UUID("11111111-1111-1111-1111-111111111111")


def _run(**overrides: object) -> AgentRunRecord:
    base = dict(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        tenant_id=_TID,
        request_id="req-1",
        agent_name="content",
        trigger="generate",
        status="completed",
        started_at=_TS,
        created_at=_TS,
    )
    base.update(overrides)
    return AgentRunRecord.model_validate(base)


def test_t89_trigger_vocabulary() -> None:
    for t in ("generate", "correct", "embed", "vision"):
        _run(trigger=t)
    for bad in ("regenerate", "bulk_import"):
        with pytest.raises(ValidationError):
            _run(trigger=bad)


def test_agent_name_vocabulary() -> None:
    for name in (
        "content",
        "keyword",
        "attribute",
        "image",
        "correction",
        "embedding",
        "quality",
    ):
        _run(agent_name=name)
    with pytest.raises(ValidationError):
        _run(agent_name="publish")


def test_agent_run_record_cost_usd_numeric_12_6() -> None:
    _run(cost_usd=Decimal("123456.123456"))
    with pytest.raises(ValidationError):
        _run(cost_usd=Decimal("1234567.123456"))
    with pytest.raises(ValidationError):
        _run(cost_usd=Decimal("1.1234567"))
    dumped = json.loads(_run(cost_usd=Decimal("1.5")).model_dump_json())
    assert dumped["cost_usd"] == "1.500000"


def test_agent_run_record_counts_non_negative() -> None:
    _run(tokens={"input": 0, "output": 0, "cached_input": 0})
    with pytest.raises(ValidationError):
        _run(tokens={"input": -1, "output": 0, "cached_input": 0})


def test_agent_run_record_attempt_bounds() -> None:
    _run(attempt=3)
    with pytest.raises(ValidationError):
        _run(attempt=0)
    with pytest.raises(ValidationError):
        _run(attempt=4)


def test_agent_run_record_carries_tenant_id() -> None:
    assert _run().tenant_id == _TID


def test_agent_run_record_has_no_credential_fields() -> None:
    forbidden = {"api_key", "credentials", "secret"}
    for name in AgentRunRecord.model_fields:
        assert name not in forbidden
    schema = json.dumps(AgentRunRecord.model_json_schema())
    for word in forbidden:
        assert f'"{word}"' not in schema
