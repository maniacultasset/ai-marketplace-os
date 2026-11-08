"""The stub `read_size_chart` (SI12 T2): deterministic, no clock, no I/O beyond its fixture.

It answers the same three-row chart (`fixtures/size_chart.json`) for any image, with one
`AgentRunRecord` of the shape the real pipeline writes and no cost, so the backend's own
normalisation and suggestion flow run end to end with no model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from typing import Final
from uuid import uuid5

from mkos_schemas.agent import AgentRunRecord, TokenUsage
from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.size_chart_read import (
    SizeChartReadChart,
    SizeChartReadRequest,
    SizeChartReadResult,
)

PROMPT_VERSION: Final = "size_chart_read/stub"
STUB_CONFIDENCE: Final = 0.9
STUB_EPOCH: Final = datetime(2026, 1, 1, tzinfo=UTC)


def _chart() -> SizeChartReadChart:
    raw = files("mkos_pipeline").joinpath("fixtures/size_chart.json").read_text(encoding="utf-8")
    return SizeChartReadChart.model_validate(json.loads(raw))


def _record(request: SizeChartReadRequest, model: str) -> AgentRunRecord:
    digest = hashlib.sha256(f"{PROMPT_VERSION}{request.image.image_id}".encode()).hexdigest()
    return AgentRunRecord(
        id=uuid5(request.job_id, f"size_chart_read:{request.attempt}"),
        tenant_id=request.tenant_id,
        product_id=request.product_id,
        request_id=request.request_id,
        job_id=request.job_id,
        agent_name="image",
        trigger="vision",
        attempt=request.attempt,
        model_used=model,
        prompt_version=PROMPT_VERSION,
        tokens=TokenUsage(),
        cost_usd=Decimal("0"),
        status="completed",
        input_digest=digest,
        started_at=STUB_EPOCH,
        created_at=STUB_EPOCH,
    )


async def read_size_chart(
    request: SizeChartReadRequest, credentials: ModelCredentials
) -> SizeChartReadResult:
    return SizeChartReadResult(
        chart=_chart(),
        confidence=STUB_CONFIDENCE,
        unreadable=False,
        agent_runs=[_record(request, credentials.model)],
    )
