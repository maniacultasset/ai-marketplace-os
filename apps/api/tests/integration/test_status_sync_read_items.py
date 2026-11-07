"""SI4 A16/A17 (AC13), round 3 F2 re-check: a read that carries every issue (`ListingRead.errors`)
opens one issue row per issue through the real status sync, so a second ERROR on one path and a
second item-level ERROR are their own rows and ride on the rejection's report."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from app.adapters.error_map import ListingRead
from mkos_schemas.enums import Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.publish import FieldResult

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, pub
from .test_status_sync_issues import _error, _issues, _published
from .test_status_sync_job import _sku, _sync

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]


class FullReads:
    """The FakeAdapter's read as a real reader returns it: a `ListingRead` whose `fields` fold
    the issues to one result per path (first error wins) and whose `errors` keep every issue."""

    def __init__(self, pub: PubWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        self.errors: dict[str, list[ErrorItem]] = {}
        real = pub.fake.get_listing

        async def get_listing(seller_sku: str, *, listing_id: uuid.UUID | None = None) -> Any:
            got = await real(seller_sku, listing_id=listing_id)
            fields = {f.field: f for f in got.fields}
            issues = self.errors.get(seller_sku, [])
            for error in issues:
                held = fields.get(error.field)
                if held is not None and held.error is not None:
                    continue
                status = "succeeded" if error.severity is Severity.WARNING else "failed"
                fields[error.field] = FieldResult(field=error.field, status=status, error=error)
            data = got.model_dump()
            data["fields"] = sorted(fields.values(), key=lambda f: f.field)
            return ListingRead(**data, errors=list(issues))

        monkeypatch.setattr(pub.fake, "get_listing", get_listing)


async def test_every_read_error_opens_its_own_row_and_rides_on_the_rejection(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = FullReads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    pub.fake.read_states[sku] = "rejected"
    reads.errors[sku] = [
        _error("content.title", "90220"),
        _error("content.title", "90221"),
        _error("listing", "77777"),
        _error("listing", "77778"),
        _error("content.description", "18001", warning=True),
    ]
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "marketplace_rejected")
    got = sorted(
        (i["field_path"], i["marketplace_code"], i["severity"], i["status"])
        for i in _issues(pub, made.listing_id)
    )
    assert got == [
        ("content.description", "18001", "warning", "open"),
        ("content.title", "90220", "error", "open"),
        ("content.title", "90221", "error", "open"),
        ("listing", "77777", "error", "open"),
        ("listing", "77778", "error", "open"),
    ]
    [sub] = pub.submissions(made.listing_id)
    assert sub["outcome"] == "rejected"
    reported = sorted((e["field"], e["raw_code"]) for e in sub["error_report_json"]["errors"])
    assert reported == [
        ("content.description", "18001"),
        ("content.title", "90220"),
        ("content.title", "90221"),
        ("listing", "77777"),
        ("listing", "77778"),
    ]


async def test_a_warning_only_read_keeps_a_published_listing_live_and_clears_later(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared blocking rule: a WARNING read item opens a warning row and never moves or
    blocks the listing; a later clean read clears the second ERROR on a path too."""
    reads = FullReads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    reads.errors[sku] = [_error("content.description", "18001", warning=True)]
    await _sync(pub, made.listing_id)
    assert pub.listing(made.listing_id)["status"] == "published"
    [warning] = _issues(pub, made.listing_id)
    assert (warning["severity"], warning["status"]) == ("warning", "open")
    reads.errors[sku] = [_error("content.title", "90220"), _error("content.title", "90221")]
    await _sync(pub, made.listing_id)
    assert pub.listing(made.listing_id)["status"] == "published"
    open_codes = sorted(
        i["marketplace_code"] for i in _issues(pub, made.listing_id) if i["status"] == "open"
    )
    assert open_codes == ["90220", "90221"]
    reads.errors.clear()
    await _sync(pub, made.listing_id)
    assert all(i["status"] == "resolved" for i in _issues(pub, made.listing_id))
