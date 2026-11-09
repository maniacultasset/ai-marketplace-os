"""SI13 T12: the stub `complete_json` answers the diagnosis fixture with one zero-cost record."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from mkos_pipeline import complete_json
from mkos_schemas.agent import AgentRunRecord
from mkos_schemas.jobs import ModelCredentials
from pydantic import BaseModel, SecretStr


class Out(BaseModel):
    plain_cause: str
    lane_class: str
    error_code: str | None
    error_class: str | None
    field_hint: str | None
    suggested_fix: str


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=uuid.uuid4(),
        product_id=None,
        listing_id=None,
        request_id="11111111-1111-4111-8111-111111111111",
        job_id=uuid.uuid4(),
    )


async def test_answers_the_fixture_with_one_record() -> None:
    records: list[AgentRunRecord] = []
    creds = ModelCredentials(provider="openai", api_key=SecretStr("x"), model="stub-model")
    out = await complete_json(
        credentials=creds,
        purpose="generation",
        prompt_version="error-diagnosis-1",
        system="s",
        developer="d",
        data={"message": "m"},
        response_model=Out,
        agent_name="correction",
        trigger="correct",
        attempt=1,
        records=records,
        ctx=_ctx(),
    )
    assert isinstance(out, Out) and out.lane_class == "seller"
    (record,) = records
    assert (record.agent_name, record.trigger, str(record.cost_usd)) == (
        "correction",
        "correct",
        "0",
    )


async def test_unknown_prompt_version_raises() -> None:
    creds = ModelCredentials(provider="openai", api_key=SecretStr("x"), model="m")
    with pytest.raises(LookupError):
        await complete_json(
            credentials=creds,
            purpose="generation",
            prompt_version="nope",
            system="",
            developer="",
            data={},
            response_model=Out,
            agent_name="correction",
            trigger="correct",
            attempt=1,
            records=[],
            ctx=_ctx(),
        )
