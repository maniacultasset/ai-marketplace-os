"""SI7 T10 (AC3, AC17 backend side): the stub `edit_image` echoes the input, deterministically."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

from mkos_pipeline import edit_image
from mkos_pipeline.image_edit import PROMPT_VERSION, STUB_COST
from mkos_schemas.codes import ErrorCode
from mkos_schemas.image_edit import ImageEditRequest, ImageEditResult
from mkos_schemas.jobs import ModelCredentials
from mkos_schemas.pipeline import edit_image as contract

CREDS = ModelCredentials(provider="openai", api_key="local-key", model="google/x-image")


def _request(**changes: Any) -> ImageEditRequest:
    path = Path(__file__).resolve().parents[2] / "schemas" / "fixtures" / "ImageEditRequest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    return ImageEditRequest.model_validate(raw)


def _run(request: ImageEditRequest) -> ImageEditResult:
    return asyncio.run(edit_image(request, CREDS))


def test_signature_matches_contract() -> None:
    assert inspect.iscoroutinefunction(edit_image)
    want = inspect.signature(contract, eval_str=True)
    assert inspect.signature(edit_image, eval_str=True) == want


def test_echo_is_byte_identical_with_one_record() -> None:
    request = _request()
    result = _run(request)
    assert result.status == "ok" and result.mime == "image/jpeg"
    assert result.image_b64 == request.image.data_b64
    (record,) = result.agent_runs
    assert (record.agent_name, record.trigger, record.prompt_version) == (
        "image",
        "vision",
        PROMPT_VERSION,
    )
    assert record.model_used == "google/x-image" and record.cost_usd == STUB_COST
    assert (record.tokens.input, record.tokens.output) == (300, 1290)
    assert record.job_id == request.job_id and record.tenant_id == request.tenant_id
    assert record.input_digest is not None and request.image.data_b64 not in record.input_digest


def test_deterministic_over_two_calls() -> None:
    assert _run(_request()).model_dump_json() == _run(_request()).model_dump_json()


def test_credit_scenario() -> None:
    result = _run(_request(category="stub:credit"))
    assert result.status == "failed" and result.error is not None
    assert result.error.error_code is ErrorCode.BUDGET_EXCEEDED
    assert "HTTP 402" in result.error.errors[0].message
    assert result.agent_runs == []


def test_no_image_scenario() -> None:
    result = _run(_request(category="stub:noimage"))
    assert result.status == "failed" and result.error is not None
    assert result.error.error_code is ErrorCode.GENERATION_FAILED
    assert result.image_b64 is None and len(result.agent_runs) == 1


def test_results_revalidate() -> None:
    for category in ("kurta", "stub:credit", "stub:noimage"):
        result = _run(_request(category=category))
        assert ImageEditResult.model_validate_json(result.model_dump_json()) == result
