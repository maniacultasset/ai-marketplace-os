"""SI14 T10: `trust_records` takes the agent, the triggers and an optional product."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.services.image_edit_result import trust_records
from mkos_schemas.agent import AgentRunRecord, TokenUsage

TENANT, JOB, PRODUCT = uuid4(), uuid4(), uuid4()
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


def _record(**changes: Any) -> AgentRunRecord:
    raw: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": TENANT,
        "product_id": PRODUCT,
        "request_id": "r",
        "job_id": JOB,
        "agent_name": "image",
        "trigger": "vision",
        "attempt": 1,
        "model_used": "m",
        "prompt_version": "size_chart_read/v1",
        "tokens": TokenUsage(),
        "cost_usd": Decimal("0.001"),
        "status": "completed",
        "input_digest": "a" * 64,
        "started_at": NOW - timedelta(seconds=5),
        "created_at": NOW,
    }
    return AgentRunRecord(**(raw | changes))


def _trust(records: list[AgentRunRecord], **kw: Any) -> Any:
    base: dict[str, Any] = {
        "prompt_prefix": "size_chart_read/",
        "tenant_id": TENANT,
        "product_id": PRODUCT,
        "job_id": JOB,
        "request_id": "job-r",
        "attempt": 2,
        "claim_started_at": NOW - timedelta(minutes=1),
        "now": NOW,
    }
    return trust_records(records, **(base | kw))


def test_si12_call_shape_unchanged() -> None:
    (out,) = _trust([_record()])
    assert (out.product_id, out.attempt, out.request_id, out.listing_id) == (
        PRODUCT,
        2,
        "job-r",
        None,
    )
    assert _trust([_record(agent_name="attribute")]) == "agent"


def test_attribute_agent_accepted_and_image_refused() -> None:
    record = _record(agent_name="attribute", trigger="generate", prompt_version="classify/v1")
    kw = {
        "prompt_prefix": "classify/",
        "agent_name": "attribute",
        "triggers": {"generate", "vision"},
    }
    assert len(_trust([record], **kw)) == 1
    assert _trust([_record(prompt_version="classify/v1")], **kw) == "agent"


def test_wrong_trigger_refused() -> None:
    record = _record(agent_name="attribute", trigger="correct", prompt_version="classify/v1")
    kw = {
        "prompt_prefix": "classify/",
        "agent_name": "attribute",
        "triggers": {"generate", "vision"},
    }
    assert _trust([record], **kw) == "trigger"


def test_no_product_restamps_none() -> None:
    record = _record(
        agent_name="attribute", trigger="generate", prompt_version="map_columns/v1", product_id=None
    )
    (out,) = _trust(
        [record],
        prompt_prefix="map_columns/",
        agent_name="attribute",
        triggers={"generate"},
        product_id=None,
    )
    assert out.product_id is None
    stray = _record(
        agent_name="attribute",
        trigger="generate",
        prompt_version="map_columns/v1",
        product_id=uuid4(),
    )
    (restamped,) = _trust(
        [stray], prompt_prefix="map_columns/", agent_name="attribute", product_id=None
    )
    assert restamped.product_id is None  # the job's identity wins


def test_foreign_identity_refused() -> None:
    assert _trust([_record(tenant_id=uuid4())]) == "identity"
    assert _trust([_record(job_id=UUID(int=1))]) == "identity"
