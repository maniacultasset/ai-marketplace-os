"""Client feedback 11 Nov (item 3): `GET /skus/availability` and `POST /skus/suggestion` as
app_rw: the live check, the made SKU, its uniqueness under concurrency and tenant isolation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from . import factories as f
from .assist_support import seed_templates
from .conftest import LOCAL_HOSTS
from .images_support import headers, images_db
from .jobs_support import JobWorld, as_member, bad_token, count

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
CHECK = "/api/v1/skus/availability"
SUGGEST = "/api/v1/skus/suggestion"


async def _check(
    api: httpx.AsyncClient, jw: JobWorld, sku: str, email: str = "reviewer@acme.test"
) -> httpx.Response:
    return await api.get(CHECK, params={"sku": sku}, headers=as_member(jw, email))


async def _suggest(
    api: httpx.AsyncClient,
    jw: JobWorld,
    body: dict[str, Any] | None = None,
    *,
    email: str = "operator@acme.test",
    key: str | None = None,
) -> httpx.Response:
    return await api.post(
        SUGGEST,
        json={"category": "tshirts"} if body is None else body,
        headers=headers(jw, email, key),
    )


@pytest.fixture(autouse=True)
def _templates(images_db: JobWorld) -> None:
    seed_templates(images_db.conn, "tshirts", "ethnic_wear_kurta_sets", "home_decor_lamps")


def _code(response: httpx.Response) -> str:
    return str(response.json()["error"]["type"])


# ---- the check ----


def _style_with_child(conn: Any, tenant: uuid.UUID) -> uuid.UUID:
    """A size style `KS-1` and its live child `KS-1-S` (as `images_support.style_and_child`)."""
    with conn.transaction():
        style = f.product(
            conn, tenant, "style", sku="KS-1", title="Kurta set", variation_theme="size",
            size_run=["s"], category="kurtas",
        )  # fmt: skip
        child = f.product(
            conn, tenant, "child", parent=style, sku="KS-1-S", category="kurtas",
            variant_attributes={"size": "s"}, attributes={"size": f.fact(value="S", normalized="s")},
        )  # fmt: skip
        conn.execute(
            "INSERT INTO variant_matrix_cells (tenant_id, style_product_id, colourway_id, size_key, "
            "availability, unavailable_reason, variant_product_id, source) "
            "VALUES (%s, %s, NULL, 's', 'available', NULL, %s, 'seller')",
            [tenant, style, child],
        )
    return style


@pytest.mark.endpoint("skus_availability", case="success")
async def test_free_taken_variant_and_deleted(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    tenant = images_db.tenant("acme")
    style = _style_with_child(images_db.conn, tenant)
    gone = f.product(images_db.conn, tenant, sku="OLD-1")
    images_db.conn.execute(
        "UPDATE products SET deleted_at = %s WHERE id = %s", [datetime.now(UTC), gone]
    )
    free = await _check(api, images_db, "KS-2")
    assert free.status_code == 200, free.text
    assert free.json() == {
        "sku": "KS-2",
        "available": True,
        "product_id": None,
        "title": None,
        "is_variant": False,
        "deleted": False,
    }
    taken = (await _check(api, images_db, "KS-1")).json()
    assert taken["available"] is False and taken["product_id"] == str(style)
    assert taken["title"] == "Kurta set" and taken["is_variant"] is False
    child = (await _check(api, images_db, "KS-1-S")).json()
    assert child["available"] is False and child["is_variant"] is True
    deleted = (await _check(api, images_db, "OLD-1")).json()
    assert deleted["available"] is False and deleted["deleted"] is True
    assert deleted["product_id"] is None


@pytest.mark.endpoint("skus_availability", case="validation")
@pytest.mark.parametrize(
    "params", [{"sku": "has space"}, {"sku": "a]b"}, {"sku": "x" * 65}, {"sku": "A\x00B"}, {}]
)
async def test_bad_sku_422(
    images_db: JobWorld, api: httpx.AsyncClient, params: dict[str, str]
) -> None:
    response = await api.get(CHECK, params=params, headers=as_member(images_db, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("skus_availability", case="wrong_tenant")
async def test_another_brands_sku_reads_free(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    f.product(images_db.conn, images_db.tenant("bharat"), sku="BH-1", title="Their product")
    response = await _check(api, images_db, "BH-1")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True and body["product_id"] is None and body["title"] is None


@pytest.mark.endpoint("skus_availability", case="missing_auth")
async def test_check_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(CHECK, params={"sku": "A-1"})).status_code == 401


@pytest.mark.endpoint("skus_availability", case="invalid_token", variant="expired")
@pytest.mark.endpoint("skus_availability", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("skus_availability", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_check_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    response = await api.get(CHECK, params={"sku": "A-1"}, headers=bad_token(images_db, variant))
    assert response.status_code == 401


# ---- the suggestion ----


@pytest.mark.endpoint("skus_suggest", case="success")
async def test_suggestions_count_up_and_a_replay_answers_the_same(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    first = await _suggest(api, images_db, key=key)
    assert first.status_code == 200, first.text
    assert first.json() == {"sku": "ACME-TSH-0001"}
    again = await _suggest(api, images_db, key=key)
    assert again.json() == {"sku": "ACME-TSH-0001"}
    second = await _suggest(api, images_db)
    assert second.json() == {"sku": "ACME-TSH-0002"}
    kurta = await _suggest(api, images_db, {"category": "ethnic_wear_kurta_sets"})
    assert kurta.json() == {"sku": "ACME-KUR-0001"}
    none = await _suggest(api, images_db, {})
    assert none.json() == {"sku": "ACME-GEN-0001"}
    own = await _suggest(api, images_db, {"category": "tshirts", "brand_name": "Zara Kids"})
    assert own.json() == {"sku": "ZARA-TSH-0001"}
    # nothing but the counters is written: no product holds a made SKU
    assert count(images_db.conn, "SELECT count(*) FROM products WHERE sku LIKE 'ACME-%%'") == 0
    assert count(images_db.conn, "SELECT count(*) FROM sku_counters") == 4


async def test_a_typed_sku_is_skipped_and_a_number_is_never_reused(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    tenant = images_db.tenant("acme")
    f.product(images_db.conn, tenant, sku="ACME-TSH-0001")
    f.product(images_db.conn, tenant, sku="ACME-TSH-0002")
    made = await _suggest(api, images_db)
    assert made.json() == {"sku": "ACME-TSH-0003"}
    # the typed SKUs' products are deleted: their numbers are still never offered again
    images_db.conn.execute("UPDATE products SET deleted_at = now() WHERE tenant_id = %s", [tenant])
    assert (await _suggest(api, images_db)).json() == {"sku": "ACME-TSH-0004"}


async def test_concurrent_suggestions_are_all_different(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    answers = await asyncio.gather(*(_suggest(api, images_db) for _ in range(8)))
    assert {r.status_code for r in answers} == {200}
    skus = [r.json()["sku"] for r in answers]
    assert len(set(skus)) == 8
    assert sorted(skus) == [f"ACME-TSH-{n:04d}" for n in range(1, 9)]


@pytest.mark.endpoint("skus_suggest", case="wrong_tenant")
async def test_each_brand_counts_on_its_own(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _suggest(api, images_db)).json() == {"sku": "ACME-TSH-0001"}
    theirs = await _suggest(api, images_db, email="operator@bharat.test")
    assert theirs.json() == {"sku": "BHAR-TSH-0001"}
    assert (await _suggest(api, images_db)).json() == {"sku": "ACME-TSH-0002"}


@pytest.mark.endpoint("skus_suggest", case="validation")
@pytest.mark.parametrize(
    "body", [{"category": "Bad-Slug"}, {"brand_name": "x" * 201}, {"unknown": "x"}]
)
async def test_bad_body_422(
    images_db: JobWorld, api: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    assert (await _suggest(api, images_db, body)).status_code == 422


@pytest.mark.parametrize("category", ["sarees", "home_decor_lamps", "uncategorized"])
async def test_unknown_or_switched_off_category_422_makes_no_counter(
    images_db: JobWorld, api: httpx.AsyncClient, category: str
) -> None:
    # sarees has no current template; lamps is off while CATEGORY_LAMPS_ENABLED is off
    response = await _suggest(api, images_db, {"category": category})
    assert response.status_code == 422, response.text
    assert _code(response) == "VALIDATION_FAILED"
    assert count(images_db.conn, "SELECT count(*) FROM sku_counters") == 0
    assert count(images_db.conn, "SELECT count(*) FROM idempotency_keys") == 0


async def test_missing_key_422(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    hdrs = headers(images_db)
    hdrs.pop("Idempotency-Key")
    assert (await api.post(SUGGEST, json={}, headers=hdrs)).status_code == 422


@pytest.mark.endpoint("skus_suggest", case="conflict")
async def test_key_reused_with_another_body_409(
    images_db: JobWorld, api: httpx.AsyncClient
) -> None:
    key = str(uuid.uuid4())
    assert (await _suggest(api, images_db, key=key)).status_code == 200
    reused = await _suggest(api, images_db, {"category": "ethnic_wear_kurta_sets"}, key=key)
    assert reused.status_code == 409 and _code(reused) == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.endpoint("skus_suggest", case="wrong_role")
async def test_reviewer_403_owner_200(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await _suggest(api, images_db, email="reviewer@acme.test")).status_code == 403
    assert (await _suggest(api, images_db, email="owner@acme.test")).status_code == 200


@pytest.mark.endpoint("skus_suggest", case="missing_auth")
async def test_suggest_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    response = await api.post(SUGGEST, json={}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status_code == 401


@pytest.mark.endpoint("skus_suggest", case="invalid_token", variant="expired")
@pytest.mark.endpoint("skus_suggest", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("skus_suggest", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_suggest_invalid_token(
    images_db: JobWorld, api: httpx.AsyncClient, variant: str
) -> None:
    hdrs = {**bad_token(images_db, variant), "Idempotency-Key": str(uuid.uuid4())}
    assert (await api.post(SUGGEST, json={}, headers=hdrs)).status_code == 401
