"""Client feedback 11 Nov (item 5): `GET /catalog/categories` as app_rw."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .assist_support import set_switch
from .conftest import LOCAL_HOSTS
from .images_support import images_db
from .jobs_support import JobWorld, as_member, bad_token
from .ops_support import category_template

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
URL = "/api/v1/catalog/categories"


def _seed(jw: JobWorld) -> None:
    for marketplace, category, product_type in (
        ("amazon", "ethnic_wear_kurta_sets", "KURTA"),
        ("flipkart", "ethnic_wear_kurta_sets", "kurta"),
        ("amazon", "tshirts", "SHIRT"),
        ("flipkart", "tshirts", "t_shirt"),
        ("amazon", "home_decor_lamps", "LAMP"),
        ("flipkart", "home_decor_lamps", "table_lamp"),
    ):
        category_template(
            jw.conn, marketplace=marketplace, category=category, product_type=product_type,
            version=2,
        )  # fmt: skip
    # an old version is never shown
    category_template(
        jw.conn, marketplace="amazon", category="tshirts", product_type="OLD", version=1,
        is_current=False,
    )  # fmt: skip


@pytest.mark.endpoint("catalog_categories_list", case="success")
async def test_categories_with_their_marketplace_mapping(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(images_db)
    set_switch(monkeypatch, "lamps", on=False)
    set_switch(monkeypatch, "category", on=True)
    response = await api.get(URL, headers=as_member(images_db, "reviewer@acme.test"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggest_enabled"] is True
    assert [(c["category"], c["label"], c["enabled"]) for c in body["items"]] == [
        ("ethnic_wear_kurta_sets", "Kurta sets", True),
        ("home_decor_lamps", "Lamps", False),
        ("tshirts", "T-shirts", True),
    ]
    kurta = body["items"][0]["marketplaces"]
    assert [(m["marketplace"], m["product_type"], m["product_type_name"]) for m in kurta] == [
        ("amazon", "KURTA", "Kurta"),
        ("flipkart", "kurta", "Kurta"),
    ]
    assert all(m["external_category_id"] is None for m in kurta)
    tees = body["items"][2]["marketplaces"]
    assert [m["product_type_name"] for m in tees] == ["Shirt", "T-shirt"]


async def test_lamps_switch_on_enables_lamps(
    images_db: JobWorld, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(images_db)
    set_switch(monkeypatch, "lamps", on=True)
    set_switch(monkeypatch, "category", on=False)
    body = (await api.get(URL, headers=as_member(images_db, "owner@acme.test"))).json()
    assert {c["category"]: c["enabled"] for c in body["items"]}["home_decor_lamps"] is True
    assert body["suggest_enabled"] is False


@pytest.mark.endpoint("catalog_categories_list", case="wrong_tenant")
async def test_same_answer_for_every_tenant(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    _seed(images_db)
    acme = await api.get(URL, headers=as_member(images_db, "owner@acme.test"))
    bharat = await api.get(URL, headers=as_member(images_db, "owner@bharat.test"))
    assert acme.status_code == bharat.status_code == 200
    assert acme.json() == bharat.json()  # platform rows only: no tenant data


@pytest.mark.endpoint("catalog_categories_list", case="missing_auth")
async def test_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(URL)).status_code == 401


@pytest.mark.endpoint("catalog_categories_list", case="invalid_token", variant="expired")
@pytest.mark.endpoint("catalog_categories_list", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("catalog_categories_list", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    response = await api.get(URL, headers=bad_token(images_db, variant))
    assert response.status_code == 401
    assert uuid.UUID(response.json()["error"]["correlation_id"])
