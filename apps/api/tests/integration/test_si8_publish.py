"""SI8 T10 (AC2, AC9; CP-D3): apparel without an audience is caught before approval (SI9's
marketplace check) and refused by the real publish job (the backstop); brand and audience issues
map to their facts, a retry resends only the audience triple, and the brand default is pinned in
the approval digest. Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from tests.support import fake_marketplace

from . import factories as f
from .conftest import LOCAL_HOSTS
from .failure_support import RETRYABLE_CODE, AmzWorld, amz
from .publish_support import approved, before_audience_rule, publish_one, retry, run_job
from .review_support import OPERATOR, REVIEWER, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["amz"]
AUDIENCE = ("age_range_description", "department", "target_gender")


async def _run(job_id: Any) -> None:
    try:
        await run_job(uuid.UUID(str(job_id)))
    except Exception as exc:  # RetryLater between attempts is the wrapper's signal
        assert type(exc).__name__ == "RetryLater", repr(exc)


def _sent(amz: AmzWorld) -> dict[str, Any]:
    ((_, stored),) = amz.state.spapi_items.items()
    return stored["attributes"]  # type: ignore[no-any-return]


def _statuses(amz: AmzWorld, made: Any) -> dict[str, str]:
    (sub,) = amz.pub.submissions(made.listing_id)
    return dict(
        amz.pub.rows(
            "SELECT field, status FROM publish_field_results WHERE submission_id = %s",
            [sub["id"]],
        )
    )


def _job_text(amz: AmzWorld, made: Any, job_id: Any) -> str:
    job = amz.pub.job(job_id)
    text = json.dumps(job.get("error_report_json") or job.get("error_json") or {}, default=str)
    rows = amz.pub.rows(
        "SELECT error_json FROM publish_field_results WHERE listing_id = %s", [made.listing_id]
    )
    return text + json.dumps([r[0] for r in rows], default=str)


async def _ungendered(amz: AmzWorld) -> str:
    from decimal import Decimal

    from app.services.products import NewProduct, create_product

    sku = f"SI8-{uuid.uuid4().hex[:8].upper()}"
    await create_product(
        amz.world.principal(OPERATOR),
        NewProduct(
            sku=sku,
            title="Cotton Kurta Set",
            category="ethnic_wear_kurta_sets",
            images=(),
            attributes={"color": "blue", "fabric_type": "cotton"},
            mrp=Decimal("1499.00"),
            selling_price=Decimal("999.00"),
            product_identifier=("ean", f.ean()),  # SI5 B2: only the audience is missing
        ),
    )
    return sku


async def test_kurta_without_gender_is_caught_before_approval(amz: AmzWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.review import ReviewItem, approve_batch

    made = await generated(amz.world, OPERATOR, sku=await _ungendered(amz))
    assert amz.pub.listing(made.listing_id)["status"] == "blocked"
    issues = amz.pub.rows(
        "SELECT field_path, error_code FROM listing_issues WHERE listing_id = %s "
        "AND source = 'schema' AND status = 'open'",
        [made.listing_id],
    )
    assert ("facts.gender", "MISSING_ATTRIBUTE") in issues, issues
    principal = amz.world.principal(REVIEWER)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        outcome = await approve_batch(
            uow,
            principal,
            [ReviewItem(made.listing_id, made.version_id, "approve")],
            http_key=str(uuid.uuid4()),
        )
    assert outcome.items[0].http_status != 200, outcome.items[0]
    assert amz.writes() == 0


async def test_kurta_without_gender_is_never_sent(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    sku = await _ungendered(amz)
    with monkeypatch.context() as before:
        before_audience_rule(before)
        made = await approved(amz.pub, sku=sku)  # approved before the audience rule
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.writes() == 0
    assert amz.pub.job(item["job_id"])["status"] == "failed"
    text = _job_text(amz, made, item["job_id"])
    assert "facts.gender" in text and "MISSING_ATTRIBUTE" in text


async def test_brand_audience_and_model_reach_amazon(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
    attrs = _sent(amz)
    tenant = amz.pub.conn.execute(
        "SELECT name FROM tenants WHERE id = %s", [amz.world.tenant("acme")]
    ).fetchone()
    assert tenant is not None
    assert attrs["brand"][0]["value"] == tenant[0].strip()
    assert [attrs[n][0]["value"] for n in AUDIENCE] == ["Adult", "Women", "female"]
    assert attrs["model_number"][0]["value"] == attrs["part_number"][0]["value"]
    statuses = _statuses(amz, made)
    for path in ("facts.brand_name", "facts.gender", "facts.style_number"):
        assert statuses[path] == "succeeded", path


async def test_department_issue_fails_gender_then_audience_only_retry(
    api: httpx.AsyncClient, amz: AmzWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    amz.state.spapi_reject["department"] = (RETRYABLE_CODE, "The department is invalid.")
    made = await approved(amz.pub)
    first = await publish_one(api, amz.pub, made)
    await _run(first["job_id"])
    statuses = _statuses(amz, made)
    assert statuses["facts.gender"] == "failed"
    assert statuses["content.title"] == statuses["facts.brand_name"] == "succeeded"
    amz.state.spapi_reject.clear()
    bodies: list[dict[str, Any]] = []
    original = fake_marketplace._spapi_patch

    def spy(state: Any, key: Any, body: dict[str, Any]) -> Any:
        bodies.append(body)
        return original(state, key, body)

    monkeypatch.setattr(fake_marketplace, "_spapi_patch", spy)
    response = await retry(api, amz.pub, made)
    assert response.status_code == 202, response.text
    await _run(response.json()["job_id"])
    (body,) = bodies
    assert sorted((p["op"], p["path"]) for p in body["patches"]) == [
        ("replace", f"/attributes/{name}") for name in AUDIENCE
    ]
    assert amz.pub.submissions(made.listing_id)[-1]["field_paths"] == ["facts.gender"]
    assert amz.pub.listing(made.listing_id)["status"] == "published"


async def test_brand_issue_fails_brand_name_only(api: httpx.AsyncClient, amz: AmzWorld) -> None:
    amz.state.spapi_reject["brand"] = (RETRYABLE_CODE, "The brand is not approved.")
    made = await approved(amz.pub)
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    failed = {path for path, status in _statuses(amz, made).items() if status == "failed"}
    assert failed == {"facts.brand_name"}


def _set_brand_names(conn: Any, tenant: Any, names: list[str]) -> None:
    conn.execute(
        "INSERT INTO brand_profiles (tenant_id, brand_names, provenance) VALUES (%s, %s, '{}') "
        "ON CONFLICT (tenant_id) DO UPDATE SET brand_names = EXCLUDED.brand_names",
        [tenant, names],
    )


async def test_brand_default_change_after_approval_voids_it(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)  # no own brand: the tenant name is the brand
    _set_brand_names(amz.pub.conn, amz.world.tenant("acme"), ["Other"])
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.writes() == 0
    assert amz.pub.job(item["job_id"])["status"] == "failed"
    assert "FACTS_CHANGED_SINCE_APPROVAL" in _job_text(amz, made, item["job_id"])


async def test_another_tenants_brand_change_leaves_the_approval(
    api: httpx.AsyncClient, amz: AmzWorld
) -> None:
    made = await approved(amz.pub)
    _set_brand_names(amz.pub.conn, amz.world.tenant("bharat"), ["Bharat Brand"])
    item = await publish_one(api, amz.pub, made)
    await _run(item["job_id"])
    assert amz.pub.job(item["job_id"])["status"] == "succeeded"
