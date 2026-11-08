"""SI13 T6-T7 (AC1, AC1b, AC2; P:A1, A3, D8; CP-D7, CP-D8, CP-D9, CP-D10): the category
reassignment item from a status read, the burst hold, and the seller-actionable signal over HTTP:
an open ERROR the seller fixes on the listing (`recategorize`, `edit_identifier`) carries
`lane: seller` and a message naming the action, opens no question, and the resolution read names
the action as its next step."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, Marketplace, Severity
from mkos_schemas.errors import ErrorItem

from .conftest import LOCAL_HOSTS
from .preview_support import prev, preview_jobs
from .publish_support import PubWorld, pub
from .review_support import OPERATOR, generated, run_job
from .test_status_sync_issues import Reads, _published
from .test_status_sync_job import _sku, _sync

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["prev", "pub"]


def _item(code: ErrorCode, path: str, raw: str | None, text: str) -> ErrorItem:
    meta = ERROR_CODE_META[code]
    return ErrorItem(
        field=path,
        error_code=code,
        severity=Severity.ERROR,
        message=text,
        source=ErrorSource.AMAZON_ADAPTER,
        retryable=False,
        marketplace=Marketplace.AMAZON,
        raw_code=raw,
        error_class=meta.error_class,
        retry_class=meta.retry_class,
    )


def _missing(attribute: str) -> ErrorItem:
    return _item(
        ErrorCode.MISSING_ATTRIBUTE,
        f"attributes.{attribute}",
        "90220",
        f"'{attribute}' is required but not supplied.",
    )


class TypedReads(Reads):
    """The read helper, plus the product type Amazon files each SKU under."""

    def __init__(self, pw: PubWorld, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(pw, monkeypatch)
        self.types: dict[str, str] = {}
        inner = pw.fake.get_listing

        async def get_listing(seller_sku: str, *, listing_id: uuid.UUID | None = None) -> Any:
            from app.adapters.error_map import ListingRead

            got = await inner(seller_sku, listing_id=listing_id)
            if seller_sku not in self.types:
                return got
            return ListingRead.model_validate(
                {**got.model_dump(), "product_type": self.types[seller_sku]}
            )

        monkeypatch.setattr(pw.fake, "get_listing", get_listing)


def _rows(pw: PubWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return pw.rows(
        "SELECT field_path, error_code, status, human_task_id, message FROM listing_issues "
        "WHERE listing_id = %s AND source = 'amazon_adapter' ORDER BY created_at, id",
        [listing_id],
    )


async def _lanes(api: httpx.AsyncClient, pw: PubWorld, listing_id: uuid.UUID) -> dict[str, Any]:
    page = await api.get(
        "/api/v1/issues",
        params={"listing_id": str(listing_id)},
        headers=pw.world.headers(OPERATOR),
    )
    assert page.status_code == 200, page.text
    return {
        r["field_path"]: (r["lane"], r["cause"], r["message"])
        for r in page.json()["data"]
        if r["status"] == "open" and r["severity"] == "error"
    }


async def _next(api: httpx.AsyncClient, pw: PubWorld, listing_id: uuid.UUID) -> dict[str, Any]:
    read = await api.get(
        f"/api/v1/listings/{listing_id}/resolution", headers=pw.world.headers(OPERATOR)
    )
    assert read.status_code == 200, read.text
    body: dict[str, Any] = read.json()
    return body


async def _previewed(pw: PubWorld, errors: list[ErrorItem]) -> Any:
    pw.fake.preview_errors = errors
    made = await generated(pw.world)
    for job in preview_jobs(pw, made.listing_id):
        if job["status"] == "queued":
            await run_job(job["id"])
    return made


def _no_stall(pw: PubWorld, lanes: dict[str, Any], listing_id: uuid.UUID) -> None:
    """No open ERROR is left with neither a seller-lane row nor a task (round 4, CP-D8)."""
    for path, _code, status, task, _msg in _rows(pw, listing_id):
        if status != "open":
            continue
        assert task is not None or lanes.get(path, ("",))[0] == "seller", path


# ---- AC1b: the seller-actionable signal over HTTP ----


async def test_category_code_is_a_seller_recategorize(
    api: httpx.AsyncClient, prev: PubWorld
) -> None:
    category = _item(
        ErrorCode.CATEGORY_MISMATCH,
        "facts.category",
        "4000003",
        "The product type is not valid for this item. Choose the category that matches it.",
    )
    made = await _previewed(prev, [category])
    lanes = await _lanes(api, prev, made.listing_id)
    lane, cause, message = lanes["facts.category"]
    assert (lane, cause) == ("seller", "mapping") and "category" in message
    ((_path, _code, status, task, _msg),) = _rows(prev, made.listing_id)
    assert status == "open" and task is None  # no inbox question: the action is on the listing
    body = await _next(api, prev, made.listing_id)
    assert body["state"] == "needs_you" and body["next_step"]["kind"] == "recategorize"
    _no_stall(prev, lanes, made.listing_id)


async def test_other_brand_barcode_is_a_seller_edit(api: httpx.AsyncClient, prev: PubWorld) -> None:
    from app.adapters.base import PrecheckResult

    found = _item(
        ErrorCode.IDENTIFIER_DUPLICATE,
        "facts.identifier",
        "catalog:other_brand",
        "This barcode belongs to another brand on Amazon. Check the barcode, or claim a GTIN "
        "exemption.",
    )
    prev.fake.precheck_result = PrecheckResult(items=(found,), checked=1)
    made = await _previewed(prev, [])
    lanes = await _lanes(api, prev, made.listing_id)
    lane, _cause, message = lanes["facts.identifier"]
    assert lane == "seller" and "barcode" in message
    body = await _next(api, prev, made.listing_id)
    assert body["next_step"]["kind"] == "edit_identifier"
    _no_stall(prev, lanes, made.listing_id)


# ---- AC1, AC2: the read-side signal on a live listing goes to staff (CP-D7) ----


async def test_wrong_product_type_opens_one_item_with_a_suggestion(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = TypedReads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    reads.types[sku] = "SHIRT"  # the T-shirt template's type: one mapped category has it
    await _sync(pub, made.listing_id)
    (row,) = [r for r in _rows(pub, made.listing_id) if r[1] == "CATEGORY_MISMATCH"]
    path, _code, status, task, message = row
    assert (path, status) == ("facts.category", "open")
    assert "Amazon now files this product as SHIRT." in message
    assert message.endswith("Suggested: tshirts.")
    assert task is not None  # an ever-live listing: a staff review, never a silent recategorise
    lanes = await _lanes(api, pub, made.listing_id)
    assert lanes["facts.category"][:2] == ("staff", "mapping")
    # the next read without the signal closes it (a published listing's read closes)
    del reads.types[sku]
    await _sync(pub, made.listing_id)
    assert [r[2] for r in _rows(pub, made.listing_id) if r[1] == "CATEGORY_MISMATCH"] != ["open"]


async def test_reassign_burst_holds_the_missing_rows(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    reads.errors[sku] = [_missing(a) for a in ("fabric_type", "neck_style", "sleeve_type")]
    await _sync(pub, made.listing_id)
    rows = _rows(pub, made.listing_id)
    (category,) = [r for r in rows if r[1] == "CATEGORY_MISMATCH"]
    assert "different kind of product" in category[4]
    missing = [r for r in rows if r[1] == "MISSING_ATTRIBUTE"]
    assert len(missing) == 3 and all(r[3] is None for r in missing)  # held: no question


async def test_two_missing_rows_raise_no_item(
    api: httpx.AsyncClient, pub: PubWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = Reads(pub, monkeypatch)
    made = await _published(api, pub)
    sku = _sku(pub, made.listing_id)
    reads.errors[sku] = [_missing(a) for a in ("fabric_type", "neck_style")]
    await _sync(pub, made.listing_id)
    rows = _rows(pub, made.listing_id)
    assert not [r for r in rows if r[1] == "CATEGORY_MISMATCH"]
    assert len([r for r in rows if r[1] == "MISSING_ATTRIBUTE"]) == 2
