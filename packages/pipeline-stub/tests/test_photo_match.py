"""The stub `photo_match` (client feedback item 9): deterministic, one record, title markers."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from mkos_pipeline import photo_match
from mkos_pipeline.photo_match import LAST_REQUEST, PROMPT_VERSION
from mkos_pipeline.photo_match import photo_match as module_entry
from mkos_schemas.jobs import ContextImage, ModelCredentials
from pydantic import SecretStr

JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdA"
    "SFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AJv/Z"
)


def _image(slot: str) -> ContextImage:
    return ContextImage(image_id=uuid.uuid4(), slot=slot, width=10, height=10, data_b64=JPEG)


def _request(title: str, colour: str | None = "Green", reference: bool = True) -> Any:
    return SimpleNamespace(
        tenant_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        request_id="r1",
        image=_image("secondary"),
        reference=_image("main") if reference else None,
        title=title,
        category="tshirts",
        colour=colour,
    )


def _credentials() -> ModelCredentials:
    return ModelCredentials(
        provider="openai", model="gpt-4o", api_key=SecretStr("k"), timeout_seconds=30
    )


def _run(request: Any) -> Any:
    return asyncio.run(photo_match(request, _credentials()))


def test_default_is_a_match_with_one_vision_record() -> None:
    request = _request("Cotton Kurta")
    result = _run(request)
    assert result.status == "ok" and result.verdict == "match"
    assert result.product_match == 0.95 and result.colour_match == 0.92
    (record,) = result.agent_runs
    assert record.agent_name == "image" and record.trigger == "vision"
    assert record.prompt_version == PROMPT_VERSION and record.job_id == request.job_id
    assert record.id == uuid.uuid5(request.job_id, "photo_match")
    assert [request] == LAST_REQUEST
    assert module_entry is photo_match


def test_no_colour_scores_the_product_only() -> None:
    assert _run(_request("Cotton Kurta", colour=None, reference=False)).colour_match is None


@pytest.mark.parametrize(
    ("marker", "verdict"), [("[photo-mismatch]", "mismatch"), ("[photo-unsure]", "unsure")]
)
def test_markers_drive_the_verdict(marker: str, verdict: str) -> None:
    result = _run(_request(f"Kurta {marker}"))
    assert result.verdict == verdict and result.reason


def test_failed_and_boom() -> None:
    failed = _run(_request("Kurta [photo-failed]"))
    assert failed.status == "failed" and failed.verdict is None and failed.error is not None
    assert failed.agent_runs[0].status == "failed"
    with pytest.raises(RuntimeError):
        _run(_request("Kurta [photo-boom]"))


def test_the_answer_round_trips_through_json() -> None:
    result = _run(_request("Kurta [photo-mismatch]"))
    again = type(result).model_validate_json(result.model_dump_json())
    assert again == result
