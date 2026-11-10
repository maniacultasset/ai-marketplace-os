"""The stub `read_photo_facts` (V1.1): deterministic, one record, fill targets only."""

from __future__ import annotations

import asyncio
import uuid

from mkos_pipeline import read_photo_facts
from mkos_schemas.jobs import ContextImage, ModelCredentials, PhotoTarget
from mkos_schemas.photo_facts_read import PhotoFactsReadRequest
from pydantic import SecretStr

# an 8x8 JPEG (the size-chart golden fixture's)
JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdA"
    "SFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AJv/Z"
)


def _request() -> PhotoFactsReadRequest:
    return PhotoFactsReadRequest(
        tenant_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        request_id="r1",
        images=[
            ContextImage(image_id=uuid.uuid4(), slot="main", width=10, height=10, data_b64=JPEG)
        ],
        targets=[
            PhotoTarget(key="neckline", mode="fill", allowed_values=["round"], min_confidence=0.7),
            PhotoTarget(key="fit", mode="check", allowed_values=["slim"], min_confidence=0.7),
        ],
        category="tshirts",
    )


def _credentials() -> ModelCredentials:
    return ModelCredentials(
        provider="openai", model="gpt-4o", api_key=SecretStr("k"), timeout_seconds=30
    )


def test_fill_targets_only_and_one_record() -> None:
    request = _request()
    result = asyncio.run(read_photo_facts(request, _credentials()))
    assert [(o.key, o.value, o.image_id) for o in result.observations] == [
        ("neckline", "round", request.images[0].image_id)
    ]
    (record,) = result.agent_runs
    assert (record.job_id, record.prompt_version, record.status) == (
        request.job_id,
        "photo_facts/v1",
        "completed",
    )
    again = asyncio.run(read_photo_facts(request, _credentials()))
    assert again == result
