"""MarketplacePreview and PreviewItem (S20 T17): the adapter's would-send payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mkos_schemas.base import SCHEMA_VERSION
from mkos_schemas.publish import MarketplacePreview, PreviewItem
from pydantic import ValidationError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "MarketplacePreview.json"


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def test_publish_preview_round_trip() -> None:
    raw = _raw()
    obj = MarketplacePreview.model_validate(raw)
    assert obj.model_dump(by_alias=True, mode="json") == raw
    again = MarketplacePreview.model_validate_json(obj.model_dump_json())
    assert again == obj


def test_publish_preview_product_type_may_be_null() -> None:
    raw = _raw() | {"product_type": None, "items": []}
    assert MarketplacePreview.model_validate(raw).product_type is None


@pytest.mark.parametrize("role", ["standalone", "parent", "child"])
def test_publish_preview_item_role_vocab(role: str) -> None:
    item = _raw()["items"][0] | {"item_role": role}
    assert PreviewItem.model_validate(item).item_role == role


@pytest.mark.parametrize("role", ["variant", "Parent", "", None])
def test_publish_preview_item_role_vocab_bad_refused(role: object) -> None:
    item = _raw()["items"][0] | {"item_role": role}
    with pytest.raises(ValidationError):
        PreviewItem.model_validate(item)


def test_publish_preview_no_url_keys() -> None:
    assert not [s for s in _strings(_raw()) if "http" in s.lower()]
    item = _raw()["items"][0]
    locator = item["payload"]["attributes"]["main_product_image_locator"][0]
    locator["media_location"] = "https://bucket.example/brand/a.jpg?X-Goog-Signature=x"
    with pytest.raises(ValidationError, match="image:<id>"):
        PreviewItem.model_validate(item)


def test_preview_has_no_verdict_field() -> None:
    names = set(MarketplacePreview.model_fields) | set(PreviewItem.model_fields)
    assert not names & {"passed", "publishable", "approved", "status"}


def test_publish_preview_fixture_schema_version_current() -> None:
    assert _raw()["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("version", [0, -1, "one"])
def test_publish_preview_version_positive(version: object) -> None:
    with pytest.raises(ValidationError):
        MarketplacePreview.model_validate(_raw() | {"version": version})
