"""V1.1 seam models: PhotoFactsReadRequest, PhotoFactsReadResult, ReadPhotoFacts."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from mkos_schemas.jobs import MAX_PHOTO_OBSERVATIONS
from mkos_schemas.photo_facts_read import (
    MAX_PHOTO_READ_IMAGES,
    PhotoFactsReadRequest,
    PhotoFactsReadResult,
)
from mkos_schemas.pipeline import ReadPhotoFacts
from pydantic import ValidationError

# an 8x8 JPEG (the size-chart golden fixture's)
JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdA"
    "SFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/wAALCAAIAAgBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAABv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AJv/Z"
)
IMAGE = {"image_id": str(uuid.uuid4()), "slot": "main", "width": 8, "height": 8, "data_b64": JPEG}
TARGET = {"key": "neckline", "mode": "fill", "allowed_values": ["round"], "min_confidence": 0.7}


def _request(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tenant_id": str(uuid.uuid4()),
        "product_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "request_id": "0" * 32,
        "images": [IMAGE],
        "targets": [TARGET],
        "category": "tshirts",
    }
    body.update(over)
    return body


def test_request_round_trips() -> None:
    parsed = PhotoFactsReadRequest.model_validate(_request())
    again = PhotoFactsReadRequest.model_validate_json(parsed.model_dump_json())
    assert again == parsed and again.attempt == 1


@pytest.mark.parametrize(
    "over",
    [
        {"images": []},
        {"images": [IMAGE] * (MAX_PHOTO_READ_IMAGES + 1)},
        {"targets": []},
        {"attempt": 4},
        {"category": "Not A Slug"},
    ],
)
def test_request_bounds(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PhotoFactsReadRequest.model_validate(_request(**over))


def test_result_caps_observations() -> None:
    obs = {"key": "neckline", "value": "round", "confidence": 0.9, "image_id": IMAGE["image_id"]}
    ok = PhotoFactsReadResult.model_validate({"observations": [obs] * MAX_PHOTO_OBSERVATIONS})
    assert len(ok.observations) == MAX_PHOTO_OBSERVATIONS
    with pytest.raises(ValidationError):
        PhotoFactsReadResult.model_validate({"observations": [obs] * (MAX_PHOTO_OBSERVATIONS + 1)})
    assert PhotoFactsReadResult.model_validate({}).observations == []


def test_protocol_is_exported() -> None:
    assert ReadPhotoFacts.__name__ == "ReadPhotoFacts"
