"""SI5 B3 attacks (AC22, AC23; PLAN §2.7-2.8): the seller's barcode or GTIN exemption, entered
through the products service as the seller does, reaches the fake SP-API on a standalone create;
a standalone with neither is refused at `facts.identifier` before any write, the refusal is a
terminal user-action failure (no retry loop), and a second publish of the same version is refused
the same way and still sends nothing."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .failure_support import AmzWorld, amz
from .publish_support import approved, publish_one, run_job
from .review_support import CATEGORY, OPERATOR

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]
EXTERNAL_ID = "externally_assigned_product_identifier"
EXEMPTION = "supplier_declared_has_product_identifier_exemption"


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


async def _standalone(
    amz: AmzWorld, *, identifier: tuple[str, str] | None = None, exempt: bool | None = None
) -> str:
    """A seller's standalone product, created through the products service (as `app_rw`)."""
    from app.services.products import NewProduct, create_product

    sku = f"SI5X-{uuid.uuid4().hex[:8].upper()}"
    await create_product(
        amz.pub.world.principal(OPERATOR),
        NewProduct(
            sku=sku,
            title="Cotton Kurta Set",
            category=CATEGORY,
            images=(),
            attributes={"color": "blue", "fabric_type": "cotton"},
            mrp=Decimal("1499.00"),
            selling_price=Decimal("999.00"),
            product_identifier=identifier,
            identifier_exemption=exempt,
        ),
    )
    return sku


def _stored(amz: AmzWorld, sku: str) -> dict[str, Any]:
    found = [v for (_seller, s), v in amz.state.spapi_items.items() if s == sku]
    assert found, sku
    return found[0]["attributes"]  # type: ignore[no-any-return]


def _put_bodies(amz: AmzWorld, sku: str) -> list[Any]:
    return [c for c in amz.state.calls if c["method"] == "PUT" and sku in c["path"]]


async def test_exempt_standalone_sends_the_exemption_and_no_barcode(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    sku = await _standalone(amz, exempt=True)
    assert (
        amz.pub.scalar("SELECT identifier_status FROM products WHERE sku = %s", [sku]) == "exempt"
    )
    made = await approved(amz.pub, sku=sku)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    attrs = _stored(amz, sku)
    assert attrs[EXEMPTION] == [{"value": True, "marketplace_id": "A21TJRUUN4KGV"}]
    assert EXTERNAL_ID not in attrs


async def test_upc_standalone_sends_the_upc_with_its_leading_zero(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    from app.core.identifiers import gtin_valid

    body = f"0{uuid.uuid4().int % 10**10:010d}"
    upc = next(body + d for d in "0123456789" if gtin_valid(body + d))
    sku = await _standalone(amz, identifier=("upc", upc))
    made = await approved(amz.pub, sku=sku)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    attrs = _stored(amz, sku)
    assert attrs[EXTERNAL_ID] == [{"type": "upc", "value": upc, "marketplace_id": "A21TJRUUN4KGV"}]
    assert EXEMPTION not in attrs


async def test_bare_standalone_is_refused_terminally_and_a_replay_sends_nothing(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    sku = await _standalone(amz)
    made = await approved(amz.pub, sku=sku)

    first = await publish_one(api, amz.pub, made)
    await _run(first["job_id"])
    job = amz.pub.job(first["job_id"])
    assert job["status"] not in ("succeeded", "queued", "running"), job["status"]
    assert amz.writes() == 0
    assert _put_bodies(amz, sku) == []
    report = json.dumps(
        amz.pub.rows(
            "SELECT error_report_json FROM listing_submissions WHERE listing_id = %s",
            [made.listing_id],
        ),
        default=str,
    )
    assert "IDENTIFIER_MISSING" in report
    assert '"facts.identifier"' in report
    assert sku in report and "GTIN-exempt" in report
    # the refusal is the seller's to fix (USER_ACTION): it never burns the retry budget
    assert "RETRY_EXHAUSTED" not in report
    attempts = len(amz.pub.submissions(made.listing_id))
    assert attempts <= 1, attempts

    # a second publish of the same approved version is refused the same way, still no write
    second = await publish_one(api, amz.pub, made)
    if second.get("job_id"):
        await _run(second["job_id"])
        assert amz.pub.job(second["job_id"])["status"] != "succeeded"
    else:  # refused at the API (the version already failed): an error item, not a job
        assert second["http_status"] >= 400, second
    assert amz.writes() == 0
    assert amz.pub.listing(made.listing_id)["published_version_id"] is None


async def test_preview_of_a_bare_standalone_shows_the_item_and_sends_nothing(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    sku = await _standalone(amz)
    made = await approved(amz.pub, sku=sku)
    preview = await api.get(
        f"/api/v1/listings/{made.listing_id}/preview", headers=amz.pub.world.headers(OPERATOR)
    )
    assert preview.status_code == 200, preview.text
    errors = preview.json()["report"]["errors"]
    found = [e for e in errors if e["error_code"] == "IDENTIFIER_MISSING"]
    assert [e["field"] for e in found] == ["facts.identifier"]
    assert sku in found[0]["message"]
    assert amz.writes() == 0


async def test_another_tenant_cannot_preview_the_refusal(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    sku = await _standalone(amz)
    made = await approved(amz.pub, sku=sku)
    other = await api.get(
        f"/api/v1/listings/{made.listing_id}/preview",
        headers=amz.pub.world.headers("operator@bharat.test"),
    )
    assert other.status_code == 404, other.text
    assert sku not in other.text


def test_factory_ean_is_valid_and_fresh() -> None:
    from app.core.identifiers import gtin_valid

    values = {f.ean() for _ in range(200)}
    assert len(values) == 200
    assert all(len(v) == 13 and gtin_valid(v) for v in values)
