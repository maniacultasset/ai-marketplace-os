"""SI4 A17 (AC13; CP-D13, CP-D16): a status read opens, repeats and clears adapter issues. All
reads of a listing family merge into one issue sync; a clean read clears only a published
listing's rows; a `failed` listing's sync is open-only."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorClass, ErrorSource, Marketplace, RetryClass, Severity
from mkos_schemas.errors import ErrorItem
from mkos_schemas.publish import FieldResult

from .conftest import LOCAL_HOSTS
from .publish_support import PubWorld, approved, pub, publish_and_run
from .test_status_sync_job import _sku, _sync

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["pub"]
TOKEN = "live-session-token-7f3a9c"


def _error(field: str, code: str = "90220", *, warning: bool = False) -> ErrorItem:
    return ErrorItem(
        field=field,
        error_code=ErrorCode.VALUE_NOT_ALLOWED,
        severity=Severity.WARNING if warning else Severity.ERROR,
        message="The marketplace refused the value. Marketplace says: bad value.",
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        marketplace=Marketplace.AMAZON,
        raw_code=code,
        error_class=ErrorClass.INVALID_VALUE,
        retry_class=RetryClass.USER_ACTION,
    )


class Reads:
    """The FakeAdapter's read, with field errors added per SKU (an adapter maps them, A16)."""

    def __init__(self, pub: PubWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        self.errors: dict[str, list[ErrorItem]] = {}
        self.clean: dict[str, list[str]] = {}  # paths a read reports as succeeded
        real = pub.fake.get_listing

        async def get_listing(seller_sku: str, *, listing_id: uuid.UUID | None = None) -> Any:
            got = await real(seller_sku, listing_id=listing_id)
            fields = {f.field: f for f in got.fields}
            for path in self.clean.get(seller_sku, []):
                fields[path] = FieldResult(field=path, status="succeeded")
            for error in self.errors.get(seller_sku, []):
                status = "succeeded" if error.severity is Severity.WARNING else "failed"
                fields[error.field] = FieldResult(field=error.field, status=status, error=error)
            return got.model_copy(update={"fields": sorted(fields.values(), key=lambda f: f.field)})

        monkeypatch.setattr(pub.fake, "get_listing", get_listing)


def _issues(pub: PubWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = pub.conn.execute(
        "SELECT * FROM listing_issues WHERE listing_id = %s ORDER BY created_at, id", [listing_id]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _items(pub: PubWorld, listing_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, str]]:
    rows = pub.rows(
        "SELECT seller_sku, id, item_role FROM listing_items WHERE listing_id = %s",
        [listing_id],
    )
    return {sku: (item_id, role) for sku, item_id, role in rows}


async def _published(api: httpx.AsyncClient, pub: PubWorld, sku: str | None = None) -> Any:
    made = await approved(pub, sku=sku)
    await publish_and_run(api, pub, made)
    assert pub.listing(made.listing_id)["status"] == "published"
    return made


async def test_an_item_level_rejection_opens_a_listing_issue(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    pub.fake.read_states[sku] = "rejected"
    reads.errors[sku] = [_error("listing")]
    await _sync(pub, made.listing_id)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "marketplace_rejected")
    [issue] = _issues(pub, made.listing_id)
    assert (issue["field_path"], issue["source"], issue["status"]) == (
        "listing",
        "amazon_adapter",
        "open",
    )
    assert (issue["error_class"], issue["retry_class"]) == ("invalid_value", "user_action")
    assert issue["message"].startswith("The marketplace refused")
    [sub] = pub.submissions(made.listing_id)
    assert sub["outcome"] == "rejected"
    assert [e["field"] for e in sub["error_report_json"]["errors"]] == ["listing"]
    assert sub["error_code"] == "VALUE_NOT_ALLOWED"


async def test_a_field_error_on_a_published_listing_then_a_clean_read(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    reads.errors[sku] = [_error("content.title")]
    await _sync(pub, made.listing_id)
    assert pub.listing(made.listing_id)["status"] == "published"
    [issue] = _issues(pub, made.listing_id)
    assert (issue["field_path"], issue["status"]) == ("content.title", "open")
    assert issue["listing_item_id"] == _items(pub, made.listing_id)[sku][0]
    await _sync(pub, made.listing_id)
    [again] = _issues(pub, made.listing_id)
    assert (again["id"], again["occurrences"]) == (issue["id"], 2)
    reads.errors.clear()
    await _sync(pub, made.listing_id)
    [cleared] = _issues(pub, made.listing_id)
    assert (cleared["status"], cleared["resolution"]) == ("resolved", "marketplace_cleared")


async def test_a_family_read_is_one_merged_sync(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CP-D16: child A reads an error, child B and the parent read clean; A's row stays open,
    and nothing closes until A reads clean too."""
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub, sku="KURTA-FAM-01")
    items = _items(pub, made.listing_id)
    children = sorted(sku for sku, (_id, role) in items.items() if role != "parent")
    assert len(children) >= 2
    child_a = children[0]
    reads.errors[child_a] = [_error("content.title")]
    await _sync(pub, made.listing_id)
    [issue] = _issues(pub, made.listing_id)
    assert (issue["status"], issue["listing_item_id"]) == ("open", items[child_a][0])
    await _sync(pub, made.listing_id)
    assert [(i["status"], i["occurrences"]) for i in _issues(pub, made.listing_id)] == [("open", 2)]
    reads.errors.clear()
    await _sync(pub, made.listing_id)
    assert [i["status"] for i in _issues(pub, made.listing_id)] == ["resolved"]


async def test_a_failed_listing_sync_is_open_only(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await approved(pub)
    pub.fake.set_fail_fields(
        {"content.title": ("90220", "Bad title.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await publish_and_run(api, pub, made)
    row = pub.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("failed", "publish_partial")
    [publish_row] = _issues(pub, made.listing_id)
    assert (publish_row["source"], publish_row["field_path"]) == ("amazon_adapter", "content.title")
    sku = _sku(pub, made.listing_id)
    reads.clean[sku] = ["content.title"]  # a clean read of the failed field (an older version)
    await _sync(pub, made.listing_id)
    assert [i["status"] for i in _issues(pub, made.listing_id)] == ["open"]
    reads.errors[sku] = [_error("content.description", "E2")]
    await _sync(pub, made.listing_id)
    assert sorted((i["field_path"], i["status"]) for i in _issues(pub, made.listing_id)) == [
        ("content.description", "open"),
        ("content.title", "open"),
    ]


async def test_a_read_repeats_the_publish_time_row(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await approved(pub)
    pub.fake.set_fail_fields(
        {"content.title": ("90220", "Bad title.", ErrorCode.VALUE_NOT_ALLOWED)}
    )
    await publish_and_run(api, pub, made)
    [publish_row] = _issues(pub, made.listing_id)
    reads.errors[_sku(pub, made.listing_id)] = [_error("content.title", "90220")]
    await _sync(pub, made.listing_id)
    [row] = _issues(pub, made.listing_id)
    assert (row["id"], row["occurrences"], row["status"]) == (publish_row["id"], 2, "open")


async def test_a_sync_while_publishing_writes_nothing(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.status_sync import ItemRead
    from app.services.status_sync_outcome import apply_sync

    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    pub.conn.execute(
        "UPDATE listings SET paused_at = now() WHERE id = %s", [made.listing_id]
    )  # an unsyncable listing (paused): the read writes nothing
    async with tenant_uow(pub.world.tenant("acme")) as uow:
        got = await apply_sync(
            uow,
            uuid.uuid4(),
            made.listing_id,
            [ItemRead(seller_sku=sku, state="live", errors=(_error("content.title"),))],
        )
    assert got is None
    assert _issues(pub, made.listing_id) == []
