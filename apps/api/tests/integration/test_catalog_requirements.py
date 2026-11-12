"""SI12 T20 (AC12): `GET /catalog/requirements?category=<slug>` as app_rw."""

from __future__ import annotations

import uuid

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .images_support import images_db
from .jobs_support import JobWorld, as_member, bad_token
from .ops_support import category_template

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
URL = "/api/v1/catalog/requirements"


@pytest.mark.endpoint("catalog_requirements_get", case="success")
async def test_tshirts(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    attributes = {
        "required_attributes": ["color", "fabric_type"],
        "optional_attributes": ["fit", "size"],
        "recommended_attributes": ["fit"],  # feedback item 10
    }
    for marketplace in ("amazon", "flipkart"):
        category_template(
            images_db.conn,
            marketplace=marketplace,
            category="tshirts",
            product_type="SHIRT",
            attributes={**attributes, "specs": {}},
        )
    response = await api.get(
        URL, params={"category": "tshirts"}, headers=as_member(images_db, "reviewer@acme.test")
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["category"] == "tshirts"
    assert body["amazon"] == [
        "brand_name",
        "color",
        "country_of_origin",
        "fabric_type",
        "gender",
        "identifier",
        "manufacturer",
        "mrp",
        "package_dimensions",
        "package_weight",
        "selling_price",
    ]
    assert body["flipkart"] == [
        "color",
        "country_of_origin",
        "fabric_type",
        "hsn_code",
        "manufacturer",
        "mrp",
        "package_dimensions",
        "package_weight",
        "selling_price",
    ]
    levels = {
        "color": "required",
        "fabric_type": "required",
        "fit": "recommended",
        "size": "optional",
    }
    assert body["levels"] == {"amazon": levels, "flipkart": levels}


# client item 4: the add form asks for `uncategorized` while "Let Kuinn suggest" is on
@pytest.mark.parametrize("category", ["no_such_category", "uncategorized"])
async def test_unknown_category_facts_and_extras_only(
    images_db: JobWorld, api: httpx.AsyncClient, category: str
) -> None:
    response = await api.get(
        URL,
        params={"category": category},
        headers=as_member(images_db, "owner@acme.test"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amazon"] == [
        "brand_name",
        "country_of_origin",
        "identifier",
        "manufacturer",
        "mrp",
        "package_dimensions",
        "package_weight",
        "selling_price",
    ]
    assert "color" not in body["flipkart"] and "hsn_code" in body["flipkart"]
    assert body["levels"] == {"amazon": {}, "flipkart": {}}


@pytest.mark.endpoint("catalog_requirements_get", case="validation")
@pytest.mark.parametrize("params", [{"category": "Bad-Slug"}, {"category": "a"}, {}])
async def test_bad_or_missing_slug_422(
    images_db: JobWorld, api: httpx.AsyncClient, params: dict[str, str]
) -> None:
    response = await api.get(URL, params=params, headers=as_member(images_db, "owner@acme.test"))
    assert response.status_code == 422


@pytest.mark.endpoint("catalog_requirements_get", case="wrong_tenant")
async def test_same_answer_for_every_tenant(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    params = {"category": "tshirts"}
    acme = await api.get(URL, params=params, headers=as_member(images_db, "owner@acme.test"))
    bharat = await api.get(URL, params=params, headers=as_member(images_db, "owner@bharat.test"))
    assert acme.status_code == bharat.status_code == 200
    assert acme.json() == bharat.json()  # platform rows only: no tenant data


@pytest.mark.endpoint("catalog_requirements_get", case="missing_auth")
async def test_missing_auth(images_db: JobWorld, api: httpx.AsyncClient) -> None:
    assert (await api.get(URL, params={"category": "tshirts"})).status_code == 401


@pytest.mark.endpoint("catalog_requirements_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("catalog_requirements_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("catalog_requirements_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_invalid_token(images_db: JobWorld, api: httpx.AsyncClient, variant: str) -> None:
    response = await api.get(
        URL, params={"category": "tshirts"}, headers=bad_token(images_db, variant)
    )
    assert response.status_code == 401
    assert uuid.UUID(response.json()["error"]["correlation_id"])
