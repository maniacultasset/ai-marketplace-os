"""S22 T9: the fake Flipkart listings v3 routes (believed shapes, DW1) and their controls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from tests.support.fake_marketplace import build_fake, issue_token

BASE = "http://fake.test/flipkart/sellers/listings/v3"
NOT_ISSUED = "not-issued-by-the-fake"


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake.test")


def _listing(sku: str, **attributes: Any) -> dict[str, Any]:
    values = attributes or {"title": [{"value": "T"}], "Colour": [{"value": "Navy Blue"}]}
    return {"sku": sku, "vertical": "kurta", "attributes": values}


@pytest.fixture
def app() -> Any:
    return build_fake()


async def _post(app: Any, path: str, body: Any, token: str | None = None) -> httpx.Response:
    auth = {"Authorization": f"Bearer {token or issue_token(app)}"}
    async with _client(app) as client:
        return await client.post(f"{BASE}{path}", json=body, headers=auth)


async def _get(app: Any, skus: str, token: str | None = None) -> httpx.Response:
    async with _client(app) as client:
        return await client.get(
            f"{BASE}/{skus}", headers={"Authorization": f"Bearer {token or issue_token(app)}"}
        )


async def _control(app: Any, **body: Any) -> None:
    async with _client(app) as client:
        assert (await client.post("/__control", json=body)).status_code == 200


async def test_fake_fk_create_stores_and_replays(app: Any) -> None:
    first = await _post(app, "", {"listings": [_listing("A1"), _listing("A2")]})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["request_id"].startswith("fk-req-")
    assert [(r["sku"], r["status"]) for r in body["results"]] == [
        ("A1", "SUCCESS"),
        ("A2", "SUCCESS"),
    ]
    again = await _post(app, "", {"listings": [_listing("A1")]})
    assert again.json()["results"][0]["status"] == "SUCCESS"
    assert len(app.state.fake.fk_items) == 2


async def test_fake_fk_update_only_named(app: Any) -> None:
    await _post(app, "", {"listings": [_listing("A1")]})
    response = await _post(
        app, "/update", {"listings": [{"sku": "A1", "attributes": {"title": [{"value": "New"}]}}]}
    )
    assert response.json()["results"][0]["status"] == "SUCCESS"
    (stored,) = app.state.fake.fk_items.values()
    assert stored["attributes"] == {
        "title": [{"value": "New"}],
        "Colour": [{"value": "Navy Blue"}],
    }


async def test_fake_fk_update_unknown_sku(app: Any) -> None:
    response = await _post(app, "/update", {"listings": [{"sku": "NO", "attributes": {}}]})
    (result,) = response.json()["results"]
    assert result["status"] == "FAILURE" and result["errors"][0]["code"] == "LISTING_NOT_FOUND"


async def test_fake_fk_errors_control_per_attribute(app: Any) -> None:
    await _control(app, fk_errors={"A1": {"Colour": "FK-COLOUR"}})
    response = await _post(app, "", {"listings": [_listing("A1")]})
    (result,) = response.json()["results"]
    assert result["status"] == "SUCCESS"
    assert [(e["attribute"], e["code"]) for e in result["errors"]] == [("Colour", "FK-COLOUR")]
    (stored,) = app.state.fake.fk_items.values()
    assert "Colour" not in stored["attributes"]
    # once per SKU
    again = await _post(app, "", {"listings": [_listing("A1")]})
    assert again.json()["results"][0]["errors"] == []


async def test_fake_fk_reject_sku(app: Any) -> None:
    await _control(app, fk_reject=["A2"])
    response = await _post(app, "", {"listings": [_listing("A1"), _listing("A2")]})
    statuses = {r["sku"]: r["status"] for r in response.json()["results"]}
    assert statuses == {"A1": "SUCCESS", "A2": "FAILURE"}
    assert [k[1] for k in app.state.fake.fk_items] == ["A1"]


async def test_fake_fk_every_attribute_refused_is_failure(app: Any) -> None:
    await _control(app, fk_reject_attribute={"title": ["X1", "no"], "Colour": ["X1", "no"]})
    (result,) = (await _post(app, "", {"listings": [_listing("A1")]})).json()["results"]
    assert result["status"] == "FAILURE" and app.state.fake.fk_items == {}


async def test_fake_fk_429_n_times(app: Any) -> None:
    await _control(app, fk_429=2)
    statuses = [
        (await _post(app, "", {"listings": [_listing("A1")]})).status_code for _ in range(3)
    ]
    assert statuses == [429, 429, 200]


async def test_fake_fk_after_write_applied_then_500(app: Any) -> None:
    await _control(app, fk_after_write=503)
    response = await _post(app, "", {"listings": [_listing("A1")]})
    assert response.status_code == 503
    assert len(app.state.fake.fk_items) == 1


async def test_fake_fk_unknown_token_401(app: Any) -> None:
    response = await _post(app, "", {"listings": [_listing("A1")]}, token=NOT_ISSUED)
    assert response.status_code == 401
    await _control(app, fk_auth={"mode": "denied", "times": 1})
    assert (await _post(app, "", {"listings": [_listing("A1")]})).status_code == 403


async def test_fake_fk_get_comma_list(app: Any) -> None:
    await _post(app, "", {"listings": [_listing("A1"), _listing("A2")]})
    await _control(app, fk_state={"A2": "ACTIVE"})
    listed = (await _get(app, "A1,A2")).json()["listings"]
    assert [(i["sku"], i["status"]) for i in listed] == [("A1", "INACTIVE"), ("A2", "ACTIVE")]
    assert all(i["listing_id"].startswith("LST") and i["fsn"] for i in listed)


async def test_fake_fk_get_unknown_absent(app: Any) -> None:
    await _post(app, "", {"listings": [_listing("A1")]})
    assert [i["sku"] for i in (await _get(app, "A1,NOPE")).json()["listings"]] == ["A1"]
    assert (await _get(app, "NOPE")).json() == {"listings": []}


async def test_fake_fk_calls_filtered_by_marketplace(app: Any) -> None:
    await _post(app, "", {"listings": [_listing("A1")]})
    async with _client(app) as client:
        await client.get(
            "/listings/2021-08-01/items/S1/X?marketplaceIds=A21TJRUUN4KGV",
            headers={"x-amz-access-token": issue_token(app)},
        )
        flipkart = (await client.get("/__calls", params={"marketplace": "flipkart"})).json()
        amazon = (await client.get("/__calls", params={"marketplace": "amazon"})).json()
        legacy = (await client.get("/__calls")).json()
    assert flipkart["count"] == 1 and flipkart["calls"][0]["method"] == "POST"
    assert amazon["count"] == 1 and isinstance(legacy, list) and len(legacy) == 2


async def test_fake_fk_reset_calls(app: Any) -> None:
    await _post(app, "", {"listings": [_listing("A1")]})
    await _control(app, reset_calls=True)
    async with _client(app) as client:
        assert (await client.get("/__calls", params={"marketplace": "flipkart"})).json() == {
            "count": 0,
            "calls": [],
        }


async def test_fake_fk_seller_from_grant(app: Any) -> None:
    """A token from the OAuth grant stores under its seller; another seller sees nothing."""
    state = app.state.fake
    state.access_tokens |= {"CANARYzq9-a", "CANARYzq9-b"}
    state.access_sellers.update({"CANARYzq9-a": "SELLER-A", "CANARYzq9-b": "SELLER-B"})
    await _post(app, "", {"listings": [_listing("A1")]}, token="CANARYzq9-a")
    assert (await _get(app, "A1", token="CANARYzq9-b")).json() == {"listings": []}
    assert [i["sku"] for i in (await _get(app, "A1", token="CANARYzq9-a")).json()["listings"]] == [
        "A1"
    ]
