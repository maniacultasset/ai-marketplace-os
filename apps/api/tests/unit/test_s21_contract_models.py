"""S21 T2 (D3, D4): `PublishItem.listing_version_id` and the `SubmissionPage` envelope."""

from __future__ import annotations

import uuid

import pytest
from app.api.v1.schemas.batches import PublishItem, PublishRequest
from app.api.v1.schemas.submissions import SubmissionPage
from pydantic import ValidationError


def test_publish_item_version_optional() -> None:
    listing = uuid.uuid4()
    assert PublishItem.model_validate({"listing_id": str(listing)}).listing_version_id is None
    version = uuid.uuid4()
    item = PublishItem.model_validate(
        {"listing_id": str(listing), "listing_version_id": str(version)}
    )
    assert item.listing_version_id == version


def test_publish_item_extra_key_422() -> None:
    with pytest.raises(ValidationError):
        PublishItem.model_validate({"listing_id": str(uuid.uuid4()), "attempt": 2})
    with pytest.raises(ValidationError):
        PublishRequest.model_validate({"items": [{"listing_id": str(uuid.uuid4())}], "x": 1})


def test_submission_page_is_s3_envelope() -> None:
    schema = SubmissionPage.model_json_schema()
    assert set(schema["properties"]) == {"data", "page"}
    page = schema["$defs"]["PageInfo"]
    assert set(page["properties"]) == {"next_cursor", "has_more", "limit"}
    assert "items" not in schema["properties"]
