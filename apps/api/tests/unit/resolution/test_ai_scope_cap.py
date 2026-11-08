"""SI11 part B fix round 1 (tester repro `test_r5_ai_scope_over_context_cap`; B1-2, B1-6): the
AI rewrite's targets fit `GenerationContext.target_fields` (`MAX_TARGET_FIELDS`), the payload
cannot carry more, and only the issues on a kept path are covered by the job."""

from __future__ import annotations

import uuid
from types import SimpleNamespace as N

import pytest
from app.services.resolution.run import _ai_scope
from app.workers.payloads import AutoFixPayload
from mkos_schemas.jobs import MAX_TARGET_FIELDS
from pydantic import ValidationError


def _steps(n: int) -> tuple[list[N], dict[uuid.UUID, N]]:
    rows: dict[uuid.UUID, N] = {}
    steps = []
    for i in range(n):
        issue_id = uuid.uuid4()
        rows[issue_id] = N(
            field_path=f"attributes.attr_{i:02d}", plain_message="bad", message="bad"
        )
        steps.append(N(lane="ai_field", issue_id=issue_id))
    return steps, rows


def test_the_ai_scope_fits_the_generation_context() -> None:
    steps, rows = _steps(12)
    paths, feedback = _ai_scope(N(marketplace="amazon"), steps, rows)  # type: ignore[arg-type]
    assert len(paths) == MAX_TARGET_FIELDS
    assert len(feedback) <= 5


def test_the_payload_refuses_more_targets_than_the_context_takes() -> None:
    base = {"listing_id": uuid.uuid4(), "mode": "ai_field", "base_version_id": uuid.uuid4()}
    AutoFixPayload.model_validate(
        {**base, "target_fields": [f"a{i}" for i in range(MAX_TARGET_FIELDS)]}
    )
    with pytest.raises(ValidationError):
        AutoFixPayload.model_validate(
            {**base, "target_fields": [f"a{i}" for i in range(MAX_TARGET_FIELDS + 1)]}
        )


class _Versions:
    async def get(self, listing_id: uuid.UUID, version_id: uuid.UUID) -> N:
        return N(id=version_id, generated_by_user_id=None)


class _Jobs:
    def __init__(self) -> None:
        self.specs: list[object] = []

    async def enqueue(self, spec: object) -> N:
        self.specs.append(spec)
        return N(created=True, job=N(id=uuid.uuid4()))


class _Listings:
    async def move(self, *args: object, **kwargs: object) -> None:
        return None


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Session:
    def begin_nested(self) -> _Nested:
        return _Nested()


async def test_only_issues_on_a_kept_target_are_covered_by_the_job() -> None:
    from app.services.resolution.run import _auto_fix

    steps, rows = _steps(MAX_TARGET_FIELDS + 3)
    jobs = _Jobs()
    uow = N(listing_versions=_Versions(), job_runs=jobs, listings=_Listings(), session=_Session())
    listing = N(id=uuid.uuid4(), marketplace="amazon", current_version_id=uuid.uuid4())
    made = await _auto_fix(uow, listing, N(id=uuid.uuid4()), steps, rows)  # type: ignore[arg-type]
    assert made is not None
    (spec,) = jobs.specs
    payload = AutoFixPayload.model_validate(spec.payload)  # type: ignore[attr-defined]
    assert len(payload.target_fields) == MAX_TARGET_FIELDS
    covered = {rows[i].field_path for i in payload.issue_ids}
    assert covered == set(payload.target_fields)
    assert len(payload.issue_ids) == MAX_TARGET_FIELDS
