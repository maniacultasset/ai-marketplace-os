"""S19 T1 (AC 15; D4, D11): the review, issue, task and history response models, and the approve
request's free-text checks."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from app.api.v1.schemas.batches import ApproveItem, ApproveRequest
from app.api.v1.schemas.issues import IssueOut
from app.api.v1.schemas.listings import AuditEventOut
from app.api.v1.schemas.tasks import TaskOut
from app.api.v1.surface import OPS, success_response
from pydantic import ValidationError

LISTING = uuid.uuid4()
VERSION = uuid.uuid4()


def _reject(feedback: str) -> dict[str, Any]:
    return {
        "listing_id": str(LISTING),
        "listing_version_id": str(VERSION),
        "decision": "reject",
        "reason_code": "poor_quality",
        "feedback": feedback,
    }


def test_issue_out_has_no_internal_columns() -> None:
    hidden = {
        "dedupe_key",
        "rule_id",
        "error_map_id",
        "validation_run_id",
        "last_validation_run_id",
        "job_id",
        "tenant_id",
    }
    assert not hidden & set(IssueOut.model_fields)


def test_task_out_has_no_evidence_or_answer() -> None:
    assert not {"evidence", "answer", "tenant_id"} & set(TaskOut.model_fields)


def test_audit_event_out_has_no_email_or_ip() -> None:
    assert not {"actor_email", "ip", "tenant_id"} & set(AuditEventOut.model_fields)


def test_issue_list_status_enum() -> None:
    spec = OPS["issues_list"]
    assert "status" in spec.query
    from app.api.v1.routers.issues import issues_list

    default = issues_list.__defaults__
    assert default is not None and "open" in default


@pytest.mark.parametrize("bad", ["\x00", "\x07", "\x1b"])
def test_feedback_c0_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ApproveItem.model_validate(_reject(f"too long{bad}title"))


def test_feedback_newline_tab_ok() -> None:
    item = ApproveItem.model_validate(_reject("line one\nline\ttwo"))
    assert item.feedback == "line one\nline\ttwo"


def test_feedback_devanagari_kept_as_sent() -> None:
    text = "शीर्षक बहुत लंबा है"
    assert ApproveItem.model_validate(_reject(text)).feedback == text


def test_feedback_2000_ok_2001_rejected() -> None:
    assert ApproveItem.model_validate(_reject("a" * 2000)).feedback is not None
    with pytest.raises(ValidationError):
        ApproveItem.model_validate(_reject("a" * 2001))


def test_approve_item_with_regenerate_true_rejected() -> None:
    with pytest.raises(ValidationError, match="regenerate applies to a reject only"):
        ApproveItem.model_validate(
            {
                "listing_id": str(LISTING),
                "listing_version_id": str(VERSION),
                "decision": "approve",
                "regenerate": True,
            }
        )


def test_reject_without_reason_or_feedback_rejected() -> None:
    for missing in ("reason_code", "feedback"):
        body = _reject("fine")
        del body[missing]
        with pytest.raises(ValidationError, match="needs reason_code and feedback"):
            ApproveItem.model_validate(body)


def test_reviewer_id_rejected() -> None:
    with pytest.raises(ValidationError):
        ApproveRequest.model_validate(
            {
                "items": [
                    {
                        "listing_id": str(LISTING),
                        "listing_version_id": str(VERSION),
                        "decision": "approve",
                        "reviewer_id": str(uuid.uuid4()),
                    }
                ]
            }
        )


@pytest.mark.parametrize("op_id", ["listings_get", "listings_content_patch"])
def test_listings_get_declares_etag(op_id: str) -> None:
    op = OPS[op_id]
    assert op.etag
    assert "ETag" in success_response(op)[op.status]["headers"]
