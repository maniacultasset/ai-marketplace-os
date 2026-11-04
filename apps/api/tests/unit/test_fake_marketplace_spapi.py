"""S20 T9: the fake SP-API Listings Items routes (supports AC2, 3, 6, 9-11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from tests.support.fake_marketplace import build_fake, issue_token
from tests.support.fault_transport import FaultTransport

BASE = "/listings/2021-08-01/items/A1SELLER"
Q = {"marketplaceIds": "A21TJRUUN4KGV"}


@pytest.fixture
async def fake() -> AsyncIterator[tuple[httpx.AsyncClient, str, Any]]:
    app = build_fake()
    token = issue_token(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        yield client, token, app


def _body(**attributes: Any) -> dict[str, Any]:
    values = attributes or {"item_name": [{"value": "Kurta"}], "color": [{"value": "Navy"}]}
    return {"productType": "KURTA", "requirements": "LISTING_PRODUCT_ONLY", "attributes": values}


def _h(token: str) -> dict[str, str]:
    return {"x-amz-access-token": token}


async def test_fake_spapi_put_stores_and_replays(fake: Any) -> None:
    client, token, app = fake
    first = await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    again = await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == again.json()["status"] == "ACCEPTED"
    assert first.json()["submissionId"] != again.json()["submissionId"]
    assert list(app.state.fake.spapi_items) == [("A1SELLER", "SKU-1")]
    got = await client.get(f"{BASE}/SKU-1", params=Q, headers=_h(token))
    assert got.json()["attributes"]["color"] == [{"value": "Navy"}]
    assert got.json()["summaries"][0]["status"] == ["DISCOVERABLE"]


async def test_fake_spapi_patch_only_named(fake: Any) -> None:
    client, token, app = fake
    await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    patch = {
        "productType": "KURTA",
        "patches": [
            {"op": "replace", "path": "/attributes/item_name", "value": [{"value": "New"}]}
        ],
    }
    response = await client.patch(f"{BASE}/SKU-1", params=Q, json=patch, headers=_h(token))
    assert response.json()["status"] == "ACCEPTED"
    stored = app.state.fake.spapi_items[("A1SELLER", "SKU-1")]["attributes"]
    assert stored == {"item_name": [{"value": "New"}], "color": [{"value": "Navy"}]}
    unknown = await client.patch(f"{BASE}/NOPE", params=Q, json=patch, headers=_h(token))
    assert unknown.status_code == 404


async def test_fake_spapi_issues_control(fake: Any) -> None:
    client, token, app = fake
    issue = {"code": "90220", "message": "bad", "severity": "ERROR", "attributeNames": ["color"]}
    await client.post("/__control", json={"spapi_issues": {"SKU-1": [issue]}})
    body = (await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))).json()
    assert body["status"] == "ACCEPTED" and body["issues"] == [issue]
    assert "color" not in app.state.fake.spapi_items[("A1SELLER", "SKU-1")]["attributes"]
    await client.post("/__control", json={"spapi_reject": {"item_name": ["8541", "no"]}})
    body = (await client.put(f"{BASE}/SKU-2", params=Q, json=_body(), headers=_h(token))).json()
    assert [i["attributeNames"] for i in body["issues"]] == [["item_name"]]


async def test_fake_spapi_invalid_control(fake: Any) -> None:
    client, token, app = fake
    await client.post("/__control", json={"spapi_invalid": ["SKU-1"]})
    body = (await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))).json()
    assert body["status"] == "INVALID"
    assert app.state.fake.spapi_items == {}
    everything = {"spapi_reject": {"item_name": ["X", "no"], "color": ["Y", "no"]}}
    await client.post("/__control", json=everything)
    body = (await client.put(f"{BASE}/SKU-2", params=Q, json=_body(), headers=_h(token))).json()
    assert body["status"] == "INVALID" and app.state.fake.spapi_items == {}


async def test_fake_spapi_429_n_times(fake: Any) -> None:
    client, token, _ = fake
    await client.post("/__control", json={"spapi_429": 2})
    codes = [
        (await client.get(f"{BASE}/SKU-1", params=Q, headers=_h(token))).status_code
        for _ in range(3)
    ]
    assert codes == [429, 429, 404]


async def test_fake_spapi_after_write_applied_then_500(fake: Any) -> None:
    client, token, app = fake
    await client.post("/__control", json={"spapi_after_write": {"SKU-1": 500}})
    first = await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    assert first.status_code == 500
    assert ("A1SELLER", "SKU-1") in app.state.fake.spapi_items  # applied anyway
    again = await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    assert again.status_code == 200


async def test_fake_spapi_fault_transport_applies_then_raises() -> None:
    app = build_fake()
    token = issue_token(app)
    transport = FaultTransport(httpx.ASGITransport(app), after_write=httpx.ReadTimeout("t"))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake.test") as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    assert ("A1SELLER", "SKU-1") in app.state.fake.spapi_items


async def test_fake_spapi_unknown_token_403(fake: Any) -> None:
    client, _, _ = fake
    response = await client.get(f"{BASE}/SKU-1", params=Q, headers=_h("CANARYzq9-not-issued"))
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "Unauthorized"
    assert (await client.get(f"{BASE}/SKU-1", params=Q)).status_code == 403


async def test_fake_spapi_auth_control(fake: Any) -> None:
    client, token, _ = fake
    await client.post("/__control", json={"spapi_auth": {"mode": "expired", "times": 1}})
    codes = [
        (await client.get(f"{BASE}/S", params=Q, headers=_h(token))).status_code for _ in range(2)
    ]
    assert codes == [401, 404]
    await client.post("/__control", json={"spapi_auth": {"mode": "denied", "times": 2}})
    codes = [
        (await client.get(f"{BASE}/S", params=Q, headers=_h(token))).status_code for _ in range(3)
    ]
    assert codes == [403, 403, 404]


async def test_fake_spapi_get_unknown_404(fake: Any) -> None:
    client, token, _ = fake
    response = await client.get(f"{BASE}/NOPE", params=Q, headers=_h(token))
    assert response.status_code == 404 and response.json()["errors"][0]["code"] == "NOT_FOUND"


async def test_fake_spapi_summary_and_body_controls(fake: Any) -> None:
    client, token, _ = fake
    await client.put(f"{BASE}/SKU-1", params=Q, json=_body(), headers=_h(token))
    await client.post("/__control", json={"spapi_summary": {"SKU-1": "BUYABLE"}})
    got = (await client.get(f"{BASE}/SKU-1", params=Q, headers=_h(token))).json()
    assert "BUYABLE" in got["summaries"][0]["status"]
    for mode, check in (
        ("redirect", lambda r: r.status_code == 302),
        ("html", lambda r: r.headers["content-type"].startswith("text/html")),
        ("oversize", lambda r: len(r.content) > 2 * 1024 * 1024),
    ):
        await client.post("/__control", json={"spapi_body": mode})
        assert check(await client.get(f"{BASE}/SKU-1", params=Q, headers=_h(token))), mode


async def test_fake_spapi_calls_recorded(fake: Any) -> None:
    client, token, _ = fake
    await client.put(f"{BASE}/a%2Fb", params=Q, json=_body(), headers=_h(token))
    await client.get(f"{BASE}/a%2Fb", params=Q, headers=_h(token))
    calls = (await client.get("/__calls")).json()
    assert [c["method"] for c in calls] == ["PUT", "GET"]
    assert calls[0]["raw_path"] == f"{BASE}/a%2Fb"
    assert len(calls[0]["body_sha256"]) == 64 and calls[1]["time"] >= calls[0]["time"]


async def test_fake_spapi_lwa_token_is_accepted() -> None:
    """A token from the fake's own LWA refresh is accepted by its SP-API routes."""
    app = build_fake()
    app.state.fake.refresh_tokens["R"] = ("amazon", "A1SELLER")
    cfg = app.state.config
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": "R",
            "client_id": cfg.amazon.client_id,
            "client_secret": cfg.amazon.client_secret,
        }
        token = (await client.post("/amazon/token", data=form)).json()["access_token"]
        response = await client.get(f"{BASE}/SKU", params=Q, headers=_h(token))
    assert response.status_code == 404


# ---- DW2 T7: the Product Type Definitions routes ----

PTD = "/definitions/2020-09-01/productTypes"
PTD_Q = {
    "marketplaceIds": "A21TJRUUN4KGV",
    "requirements": "LISTING_PRODUCT_ONLY",
    "locale": "en_IN",
}


async def _envelope(client: httpx.AsyncClient, token: str, product_type: str = "KURTA") -> Any:
    return await client.get(f"{PTD}/{product_type}", params=PTD_Q, headers=_h(token))


def _link_path(envelope: dict[str, Any]) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(envelope["schema"]["link"]["resource"])
    assert parts.scheme == "https" and parts.hostname == "ptd-schemas.fake.amazonaws.com"
    return f"{parts.path}?{parts.query}"


async def test_fake_ptd_envelope_and_checksum(fake: Any) -> None:
    from tests.support.fake_marketplace import ptd_checksum, ptd_fixture

    client, token, _ = fake
    reply = await _envelope(client, token)
    assert reply.status_code == 200
    envelope = reply.json()
    assert set(envelope) >= {
        "displayName",
        "locale",
        "marketplaceIds",
        "metaSchema",
        "productType",
        "productTypeVersion",
        "propertyGroups",
        "requirements",
        "requirementsEnforced",
        "schema",
    }
    assert envelope["requirementsEnforced"] == "ENFORCED"
    schema = await client.get(_link_path(envelope))
    assert schema.status_code == 200
    assert schema.content == ptd_fixture("KURTA.schema.json")
    assert envelope["schema"]["checksum"] == ptd_checksum(schema.content)


async def test_fake_ptd_us_marketplace_denied_and_tokenless_refused(fake: Any) -> None:
    client, token, _ = fake
    us = await client.get(
        f"{PTD}/KURTA", params={**PTD_Q, "marketplaceIds": "ATVPDKIKX0DER"}, headers=_h(token)
    )
    assert us.status_code == 403
    assert (await client.get(f"{PTD}/KURTA", params=PTD_Q)).status_code == 403
    assert (await _envelope(client, token, "NOPE")).status_code == 404


@pytest.mark.parametrize(
    ("switch", "check"),
    [
        ("deny", "envelope_403"),
        ("not_enforced", "not_enforced"),
        ("bad_checksum", "bad_checksum"),
        ("external_ref", "external_ref"),
        ("redirect_link", "link_302"),
        ("oversize", "link_oversize"),
    ],
)
async def test_fake_ptd_switch_applies_once(fake: Any, switch: str, check: str) -> None:
    from tests.support.fake_marketplace import PTD_OVERSIZE, ptd_checksum

    client, token, _ = fake
    await client.post("/__control", json={"ptd": {switch: True}})
    for round_ in (1, 2):
        reply = await _envelope(client, token)
        hit = round_ == 1
        if check == "envelope_403":
            assert (reply.status_code == 403) is hit
            continue
        envelope = reply.json()
        link = await client.get(_link_path(envelope))
        if check == "not_enforced":
            assert (envelope["requirementsEnforced"] == "NOT_ENFORCED") is hit
        elif check == "bad_checksum":
            assert (envelope["schema"]["checksum"] != ptd_checksum(link.content)) is hit
        elif check == "external_ref":
            assert (b"schemas.example.invalid" in link.content) is hit
        elif check == "link_302":
            assert (link.status_code == 302) is hit
        else:
            assert (len(link.content) == PTD_OVERSIZE) is hit


async def test_fake_ptd_status_list_and_reset(fake: Any) -> None:
    client, token, app = fake
    await client.post(
        "/__control", json={"ptd": {"status": [429, 500], "schema": "KURTA.drift.schema.json"}}
    )
    assert (await _envelope(client, token)).status_code == 429
    assert (await _envelope(client, token)).status_code == 500
    envelope = (await _envelope(client, token)).json()
    assert b"neckline_style" in (await client.get(_link_path(envelope))).content
    await client.post("/__control/reset", json={"ptd": True})
    assert app.state.fake.ptd == {} and app.state.fake.ptd_served == {}
    envelope = (await _envelope(client, token)).json()
    assert b"neckline_style" not in (await client.get(_link_path(envelope))).content


async def test_fake_ptd_calls_hold_header_names_only(fake: Any) -> None:
    client, token, app = fake
    envelope = (await _envelope(client, token)).json()
    await client.get(_link_path(envelope))
    calls = [c for c in app.state.fake.calls if c.get("kind") in ("ptd", "ptd_schema")]
    assert [c["kind"] for c in calls] == ["ptd", "ptd_schema"]
    assert "x-amz-access-token" in calls[0]["header_names"]
    assert token not in repr(calls)
