"""SI5 B3 (AC23; PLAN §2.8, D17; CP-D14): the real publish job and `AmazonAdapter` against the
in-process fake SP-API. A first publish of a family with one child that has neither a barcode nor
the GTIN exemption is refused before any write with IDENTIFIER_MISSING at the child's path; the
same family, once the child has an EAN, is accepted; a listing Amazon already accepted (the
`update` operation) is not refused when a child loses its barcode.

SI9 B4: the marketplace check before approval now catches the bare child, so the publish-time
refusal is proven on a version approved before the identifier rule (`before_identifier_rule`);
a live listing's check, like its update, does not refuse a missing barcode."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .family_support import family
from .publish_support import PubWorld, approved, before_identifier_rule, publish_one, run_job
from .review_support import OPERATOR, REVIEWER, Made

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]
CATEGORY = "ethnic_wear_kurta_sets"
EXTERNAL_ID = "externally_assigned_product_identifier"


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _family(pw: PubWorld) -> tuple[str, dict[str, uuid.UUID]]:
    sku = f"SI5B-{uuid.uuid4().hex[:6].upper()}"
    _style, kids = family(pw.conn, pw.world.tenant("acme"), sku, category=CATEGORY)
    return sku, kids


def _set_barcode(pw: PubWorld, product_id: uuid.UUID, value: str | None) -> None:
    """The seller's barcode on one child (None clears it), with its seller provenance."""
    if value is None:
        pw.conn.execute(
            "UPDATE products SET identifier_type = NULL, identifier_value = NULL, "
            "identifier_status = NULL, provenance = provenance - 'identifier' WHERE id = %s",
            [product_id],
        )
        return
    pw.conn.execute(
        "UPDATE products SET identifier_type = 'ean', identifier_value = %s, "
        "identifier_status = 'provided', provenance = provenance || %s WHERE id = %s",
        [value, json.dumps({"identifier": f.fact(value=None)}), product_id],
    )


async def _new_version(pw: PubWorld, made: Made) -> Made:
    """The seller's next version after a fact change: a regeneration (operator), then the
    reviewer's approval, so the approval pins the facts as they are now."""
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration
    from app.services.review import ReviewItem, approve_batch

    maker = pw.world.principal(OPERATOR)
    async with tenant_uow(maker.tenant_id, user_id=maker.user_id) as uow:
        accepted = await request_regeneration(uow, maker, str(uuid.uuid4()), made.listing_id, None)
    await run_job(accepted.job_id)
    version = pw.listing(made.listing_id)["current_version_id"]
    assert version is not None and version != made.version_id
    checker = pw.world.principal(REVIEWER)
    async with tenant_uow(checker.tenant_id, user_id=checker.user_id) as uow:
        outcome = await approve_batch(
            uow,
            checker,
            [ReviewItem(made.listing_id, version, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status == 200, outcome.items[0]
    return Made(made.listing_id, version, accepted.job_id)


def _report_text(pw: PubWorld, job_id: Any, listing_id: uuid.UUID) -> str:
    job = pw.job(job_id)
    text = json.dumps(job.get("error_report_json") or job.get("error_json") or {}, default=str)
    rows = pw.rows(
        "SELECT error_json FROM publish_field_results WHERE listing_id = %s", [listing_id]
    )
    rows += pw.rows(
        "SELECT error_report_json FROM listing_submissions WHERE listing_id = %s", [listing_id]
    )
    return text + json.dumps([r[0] for r in rows], default=str)


def _stored(amz: AmzWorld, sku: str) -> dict[str, Any]:
    found = [v for (_seller, s), v in amz.state.spapi_items.items() if s == sku]
    assert found, sku
    return found[0]["attributes"]  # type: ignore[no-any-return]


async def _check_codes(pw: PubWorld, listing_id: uuid.UUID) -> set[tuple[str, str]]:
    """The blocking items of the real SI9 marketplace check on the current version."""
    from app.repositories.uow import tenant_uow
    from app.services.adapter_check import adapter_check

    principal = pw.world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        report = await adapter_check(uow, listing_id)
    assert report is not None
    return {(e.field, e.error_code.value) for e in report.blocking}


async def test_first_publish_with_a_bare_child_is_refused_then_accepted_with_an_ean(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    sku, kids = _family(amz.pub)
    bare = sorted(kids)[0]
    _set_barcode(amz.pub, kids[bare], None)
    with monkeypatch.context() as before:
        before_identifier_rule(before)
        made = await approved(amz.pub, sku=sku)  # approved before the identifier rule
    # today's check before approval refuses it at the child's path
    assert (f"variants[{bare}].facts.identifier", "IDENTIFIER_MISSING") in await _check_codes(
        amz.pub, made.listing_id
    )

    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.writes() == 0  # refused before the send
    assert amz.pub.job(item["job_id"])["status"] != "succeeded"
    text = _report_text(amz.pub, item["job_id"], made.listing_id)
    assert "IDENTIFIER_MISSING" in text
    assert f"variants[{bare}].facts.identifier" in text
    assert "GTIN-exempt" in text
    preview = await api.get(
        f"/api/v1/listings/{made.listing_id}/preview", headers=amz.world.headers(OPERATOR)
    )
    assert preview.status_code == 200, preview.text
    shown = {(e["field"], e["error_code"]) for e in preview.json()["report"]["errors"]}
    assert (f"variants[{bare}].facts.identifier", "IDENTIFIER_MISSING") in shown

    barcode = f.ean()
    _set_barcode(amz.pub, kids[bare], barcode)
    again = await _new_version(amz.pub, made)
    item = await publish_one(api, amz.pub, again)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    (entry,) = _stored(amz, bare)[EXTERNAL_ID]
    assert (entry["type"], entry["value"]) == ("ean", barcode)
    last = amz.pub.submissions(made.listing_id)[-1]
    assert (last["operation"], last["outcome"]) == ("create", "accepted")


async def test_accepted_listing_is_not_refused_when_a_child_loses_its_barcode(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    sku, kids = _family(amz.pub)
    made = await approved(amz.pub, sku=sku)
    first = await publish_one(api, amz.pub, made)
    await _run(first["job_id"])
    assert amz.pub.job(first["job_id"])["status"] == "succeeded"
    assert amz.pub.listing(made.listing_id)["published_version_id"] is not None

    child = sorted(kids)[0]
    _set_barcode(amz.pub, kids[child], None)
    again = await _new_version(amz.pub, made)  # SI9: the live listing's check lets it through
    assert not {
        c for c in await _check_codes(amz.pub, made.listing_id) if c[1] == "IDENTIFIER_MISSING"
    }
    puts_before = sum(1 for call in amz.state.calls if call["method"] == "PUT")
    item = await publish_one(api, amz.pub, again)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    last = amz.pub.submissions(made.listing_id)[-1]
    assert (last["operation"], last["outcome"]) == ("update", "accepted")
    assert sum(1 for call in amz.state.calls if call["method"] == "PUT") == puts_before
    assert any(call["method"] == "PATCH" for call in amz.state.calls)
    assert "IDENTIFIER_MISSING" not in _report_text(amz.pub, item["job_id"], made.listing_id)
