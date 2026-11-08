"""SI7 T12: the trust check of an image-edit result (restamp and clamp)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.image_edit_result import EditTrusted, EditUntrusted, check_edit_result
from mkos_schemas.image_edit import ImageEditResult

FIXTURES = Path(__file__).resolve().parents[4] / "packages" / "schemas" / "fixtures"
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
JOB = uuid.UUID("44444444-4444-4444-4444-444444444444")
PRODUCT = uuid.uuid4()
CLAIM = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
NOW = CLAIM + timedelta(minutes=1)


def _result(**record: Any) -> ImageEditResult:
    raw = json.loads((FIXTURES / "ImageEditResult.json").read_text(encoding="utf-8"))
    raw["agent_runs"][0].update(record)
    return ImageEditResult.model_validate(raw)


def _check(result: object) -> EditTrusted | EditUntrusted:
    return check_edit_result(
        result,
        tenant_id=TENANT,
        product_id=PRODUCT,
        job_id=JOB,
        request_id="req-job",
        attempt=1,
        claim_started_at=CLAIM,
        now=NOW,
    )


def test_stub_epoch_is_clamped_to_the_claim() -> None:
    checked = _check(_result(started_at="2026-01-01T00:00:00Z"))
    assert isinstance(checked, EditTrusted)
    (record,) = checked.records
    assert record.started_at == CLAIM
    assert record.product_id == PRODUCT and record.request_id == "req-job"


def test_future_record_is_clamped() -> None:
    checked = _check(_result(started_at=(NOW + timedelta(hours=1)).isoformat()))
    assert isinstance(checked, EditTrusted) and checked.records[0].started_at == CLAIM


def test_record_inside_the_window_is_kept() -> None:
    inside = CLAIM + timedelta(seconds=30)
    checked = _check(_result(started_at=inside.isoformat()))
    assert isinstance(checked, EditTrusted) and checked.records[0].started_at == inside


def test_identity_and_shape_checks() -> None:
    assert isinstance(_check(_result(tenant_id=str(uuid.uuid4()))), EditUntrusted)
    assert isinstance(_check(_result(job_id=str(uuid.uuid4()))), EditUntrusted)
    assert isinstance(_check(_result(agent_name="content")), EditUntrusted)
    assert isinstance(_check(_result(prompt_version="photo_facts/v1")), EditUntrusted)
    assert isinstance(_check(object()), EditUntrusted)
    broken = ImageEditResult.model_construct(status="ok", image_b64="!!", mime="image/png")
    assert _check(broken) == EditUntrusted("schema")


def test_restamp_overrides_product_request_and_attempt() -> None:
    checked = _check(_result(product_id=str(uuid.uuid4()), request_id="other", attempt=3))
    assert isinstance(checked, EditTrusted)
    (record,) = checked.records
    assert (record.product_id, record.request_id, record.attempt) == (PRODUCT, "req-job", 1)
    assert checked.result.agent_runs == [record]
