"""SI7 API models: image candidates and the ProductImageOut provenance fields (AC1, AC14, AC15)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from app.api.v1.schemas.image_candidates import (
    ImageCandidateAccepted,
    ImageCandidateIn,
    ImageCandidateOut,
)
from app.api.v1.schemas.products import ProductImageOut
from pydantic import ValidationError


def _locs(error: ValidationError) -> list[tuple[Any, ...]]:
    return [tuple(e["loc"]) for e in error.errors()]


@pytest.mark.parametrize("kind", ["fix", "white_background"])
def test_photo_kind_needs_a_source(kind: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ImageCandidateIn.model_validate({"kind": kind})
    assert ("source_image_id",) in _locs(caught.value)
    assert ImageCandidateIn.model_validate({"kind": kind, "source_image_id": str(uuid4())})


def test_size_chart_refuses_a_source() -> None:
    with pytest.raises(ValidationError) as caught:
        ImageCandidateIn.model_validate({"kind": "size_chart", "source_image_id": str(uuid4())})
    assert ("source_image_id",) in _locs(caught.value)
    assert ImageCandidateIn.model_validate({"kind": "size_chart"}).source_image_id is None


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "lifestyle", "source_image_id": str(uuid4())},
        {"kind": None},
        {"kind": "size_chart", "extra": 1},
        {},
    ],
    ids=["unknown-kind", "null-kind", "extra-key", "empty"],
)
def test_bad_bodies_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ImageCandidateIn.model_validate(body)


def test_candidate_out_cost_dumps_six_places() -> None:
    out = ImageCandidateOut(
        id=uuid4(),
        kind="white_background",
        status="ready",
        cost_usd=Decimal("0.039"),
        created_at="2026-11-07T10:00:00Z",
    )
    assert out.model_dump(mode="json")["cost_usd"] == "0.039000"
    assert out.preview_url is None and out.source_preview_url is None


def test_accepted_allows_no_job() -> None:
    body = ImageCandidateAccepted(candidate_id=uuid4(), status="ready")
    assert body.job_id is None and body.poll_url is None


def test_old_product_image_body_still_parses() -> None:
    old = {
        "image_id": str(uuid4()),
        "slot": "main",
        "colourway_id": None,
        "position": 0,
        "status": "uploaded",
        "compliance_status": "passed",
    }
    image = ProductImageOut.model_validate(old)
    assert image.origin == "seller"
    assert image.parent_image_id is None
    assert image.fixes == []


def test_product_image_fixes_vocabulary() -> None:
    base = {
        "image_id": str(uuid4()),
        "slot": "main",
        "position": 0,
        "status": "uploaded",
        "compliance_status": "failed",
    }
    assert ProductImageOut.model_validate({**base, "fixes": ["fix"]}).fixes == ["fix"]
    with pytest.raises(ValidationError):
        ProductImageOut.model_validate({**base, "fixes": ["lifestyle"]})
    with pytest.raises(ValidationError):
        ProductImageOut.model_validate({**base, "origin": "ai"})


def test_create_answer_names_the_applied_image_only_when_given() -> None:
    plain = ImageCandidateAccepted.model_validate(
        {"candidate_id": str(uuid4()), "status": "queued", "job_id": str(uuid4()), "poll_url": "/x"}
    )
    assert plain.accepted_image_id is None  # a stored answer from before the field still parses
    image = uuid4()
    applied = ImageCandidateAccepted(
        candidate_id=uuid4(), status="accepted", accepted_image_id=image
    )
    assert applied.model_dump(mode="json")["accepted_image_id"] == str(image)


def test_create_answer_carries_a_signed_applied_preview_only_when_given() -> None:
    plain = ImageCandidateAccepted.model_validate(
        {"candidate_id": str(uuid4()), "status": "queued", "job_id": str(uuid4()), "poll_url": "/x"}
    )
    assert plain.applied_preview_url is None  # a stored answer never holds one
    applied = ImageCandidateAccepted(
        candidate_id=uuid4(),
        status="accepted",
        accepted_image_id=uuid4(),
        applied_preview_url="https://store.test/c.jpg?X-Amz-Signature=x",
    )
    assert applied.model_dump(mode="json")["applied_preview_url"].endswith("Signature=x")
