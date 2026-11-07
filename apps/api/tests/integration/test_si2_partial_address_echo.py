"""SI2 fix round 2 (AC5, DPDP `db/models/facts.py:88`): a marketplace issue that echoes only
part of the manufacturer contact (Amazon truncates or reformats the value) never stores the
street or city in `publish_field_results.error_json` or `listing_issues`."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .publish_support import approved, publish_one, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]


async def test_partial_echo_of_the_address_is_never_stored(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    amz.state.spapi_reject["manufacturer_contact_information"] = (
        "90220",
        "The value 'Test Mills Pvt Ltd, 1 Mill Road, Surat' is invalid.",
    )
    item = await publish_one(api, amz.pub, made)
    try:
        await run_job(uuid.UUID(str(item["job_id"])))
    except Exception as exc:
        assert type(exc).__name__ == "RetryLater", repr(exc)
    rows: list[Any] = amz.pub.rows(
        "SELECT error_json FROM publish_field_results WHERE listing_id = %s", [made.listing_id]
    )
    rows += amz.pub.rows(
        "SELECT raw_message, message FROM listing_issues WHERE listing_id = %s", [made.listing_id]
    )
    assert "1 Mill Road" not in json.dumps(rows, default=str)
