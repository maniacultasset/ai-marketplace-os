"""SubmissionOut and SubmissionItemOut (S21 T2; PLAN D3): one publish attempt as the API reads it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from mkos_schemas.publish import SubmissionItemOut, SubmissionOut, SubmissionOutcome
from pydantic import ValidationError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "SubmissionOut.json"
INTERNAL = {
    "payload_storage_key",
    "payload_sha256",
    "facts_digest",
    "idempotency_key",
    "raw_response_sha256",
    "raw_response_storage_key",
    "submission_ref",
    "corrections_json",
}


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_submission_round_trip() -> None:
    raw = _raw()
    obj = SubmissionOut.model_validate(raw)
    assert obj.model_dump(mode="json") == raw
    assert SubmissionOut.model_validate_json(obj.model_dump_json()) == obj


def test_submission_no_storage_or_url_keys() -> None:
    names = set(SubmissionOut.model_fields) | set(SubmissionItemOut.model_fields)
    assert not names & INTERNAL
    assert not any("url" in n or "storage" in n or "raw" in n for n in names)


def test_submission_field_paths_legal() -> None:
    with pytest.raises(ValidationError):
        SubmissionOut.model_validate(_raw() | {"field_paths": ["not a path"]})


def test_submission_outcome_vocab() -> None:
    assert set(get_args(SubmissionOutcome)) == {
        "pending",
        "accepted",
        "partially_accepted",
        "rejected",
        "error",
        "live",
        "superseded",
    }
    with pytest.raises(ValidationError):
        SubmissionOut.model_validate(_raw() | {"outcome": "submitted"})


@pytest.mark.parametrize("attempt", [0, 4])
def test_submission_attempt_bounded(attempt: int) -> None:
    with pytest.raises(ValidationError):
        SubmissionOut.model_validate(_raw() | {"attempt": attempt})
