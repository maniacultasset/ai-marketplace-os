"""S21 T3 (CP11; DW8, AC12): the publish-family job payloads are frozen at v1, additive only, and
an older worker ignores keys a newer API adds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.workers.payloads import PublishJobPayload, StatusSyncPayload, dump
from pydantic import BaseModel, ValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "job_payloads" / "v1"
MODELS: dict[str, type[BaseModel]] = {
    "publish": PublishJobPayload,
    "retry_fields": PublishJobPayload,
    "resume": PublishJobPayload,
    "status_sync": StatusSyncPayload,
}


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize("name", sorted(MODELS))
def test_job_payload_compat(name: str) -> None:
    raw = _fixture(name)
    parsed = MODELS[name].model_validate(raw)
    assert dump(parsed) == raw  # type: ignore[arg-type]


def test_job_payload_unknown_keys_ignored() -> None:
    raw = _fixture("publish") | {"added_in_v1_1": True}
    assert dump(PublishJobPayload.model_validate(raw)) == _fixture("publish")


@pytest.mark.parametrize("missing", ["listing_id", "listing_version_id"])
def test_job_payload_missing_required_fails(missing: str) -> None:
    raw = _fixture("publish")
    del raw[missing]
    with pytest.raises(ValidationError):
        PublishJobPayload.model_validate(raw)


def test_job_payload_v2_refused() -> None:
    with pytest.raises(ValidationError):
        PublishJobPayload.model_validate(_fixture("publish") | {"v": 2})
    with pytest.raises(ValidationError):
        StatusSyncPayload.model_validate(_fixture("status_sync") | {"v": 2})


def test_payloads_are_additive_only() -> None:
    for name, model in MODELS.items():
        assert set(_fixture(name)) <= set(model.model_fields), name
