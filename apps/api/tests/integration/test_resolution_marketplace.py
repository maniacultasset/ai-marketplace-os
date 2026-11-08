"""SI11 T18 (AC12; CP-D13): a read that says only the marketplace can act (an account or
category permission) moves a published listing to `blocked/marketplace_action_required`, opens
one `permission_decision` task with the Seller Central step, and later reads enqueue nothing."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, pub
from .test_status_sync_issues import Reads, _published
from .test_status_sync_job import _sku, _sync

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


def _gated() -> ErrorItem:
    return ErrorItem(
        field="listing",
        error_code=ErrorCode.ACCOUNT_PERMISSION_DENIED,
        severity=Severity.ERROR,
        message="You need approval to list products of this brand.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        marketplace=Marketplace.AMAZON,
        raw_code="99320",
        error_class=ErrorClass.ACCOUNT_PERMISSION,
        retry_class=RetryClass.USER_ACTION,
    )


def _jobs(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT kind, status FROM job_runs WHERE listing_id = %s AND kind = 'listing_publish' "
        "ORDER BY queued_at",
        [listing_id],
    )


async def test_a_gated_read_opens_the_seller_central_step_and_never_loops(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    publishes = _jobs(pub, made.listing_id)
    pub.fake.read_states[sku] = "rejected"
    reads.errors[sku] = [_gated()]
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "marketplace_action_required")
    tasks = pub.rows(
        "SELECT t.kind, t.target_path, t.question FROM listing_issues i "
        "JOIN human_tasks t ON t.id = i.human_task_id "
        "WHERE i.listing_id = %s AND i.status = 'open'",
        [made.listing_id],
    )
    assert [(k, t) for k, t, _q in tasks] == [
        ("permission_decision", f"account.category_approvals.{row['category']}")
    ]
    assert "Seller Central" in tasks[0][2]
    # no automatic resend now or on a later read: only the marketplace can act
    assert _jobs(pub, made.listing_id) == publishes
    await _sync(pub, made.listing_id)
    assert _jobs(pub, made.listing_id) == publishes
    live = pub.rows(
        "SELECT count(*) FROM human_tasks WHERE kind = 'permission_decision' "
        "AND status = 'open' AND tenant_id = %s",
        [pub.world.tenant("acme")],
    )
    assert live[0][0] == 1
