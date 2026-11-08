"""SI12 T2: the stub `read_size_chart` answers a fixed chart, with one record, deterministically."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import mkos_pipeline
from mkos_pipeline import read_size_chart
from mkos_pipeline.size_chart import PROMPT_VERSION
from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.size_chart_read import SizeChartReadRequest, SizeChartReadResult

SCHEMA_FIXTURES = Path(__file__).resolve().parents[2] / "schemas" / "fixtures"


def _load(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((SCHEMA_FIXTURES / f"{name}.json").read_text("utf-8"))
    return raw


def _request(**changes: Any) -> SizeChartReadRequest:
    return SizeChartReadRequest.model_validate(_load("SizeChartReadRequest") | changes)


def _creds() -> ModelCredentials:
    return ModelCredentials.model_validate(_load("ModelCredentials") | {"api_key": "k"})


def _read(request: SizeChartReadRequest) -> SizeChartReadResult:
    return asyncio.run(read_size_chart(request, _creds()))


def test_exported() -> None:
    assert "read_size_chart" in mkos_pipeline.__all__


def test_fixed_chart_for_any_image() -> None:
    first = _read(_request())
    other = _read(_request(product_id="00000000-0000-4000-8000-000000000001"))
    assert first.chart is not None and first.chart == other.chart
    assert first.chart.unit == "cm" and first.chart.basis == "garment"
    assert [row["size"] for row in first.chart.rows] == ["S", "M", "L"]
    assert not first.unreadable and first.error is None
    assert first.confidence is not None and 0 <= first.confidence <= 1


def test_one_record_with_the_request_ids() -> None:
    request = _request(attempt=2)
    result = _read(request)
    assert len(result.agent_runs) == 1
    run = result.agent_runs[0]
    assert run.tenant_id == request.tenant_id
    assert run.product_id == request.product_id
    assert run.job_id == request.job_id
    assert run.request_id == request.request_id
    assert run.attempt == 2
    assert run.agent_name == "image"
    assert run.prompt_version == PROMPT_VERSION == "size_chart_read/stub"
    assert run.cost_usd == 0
    assert run.model_used == _creds().model


def test_deterministic() -> None:
    request = _request()
    assert _read(request).model_dump(mode="json") == _read(request).model_dump(mode="json")
    assert (
        _read(request).agent_runs[0].id
        != _read(_request(job_id=UUID("00000000-0000-4000-8000-000000000002"))).agent_runs[0].id
    )
