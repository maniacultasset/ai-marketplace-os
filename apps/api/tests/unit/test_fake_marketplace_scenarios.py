"""SI11 T4 (AC2, AC4, AC7, AC11-15, AC22): the fake SP-API dry run (`mode=VALIDATION_PREVIEW`)
and the scripted rejection scenarios, which decide issues from the value actually sent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from tests.support.fake_marketplace import SCENARIOS, build_fake, issue_token

BASE = "/listings/2021-08-01/items/A1SELLER"
Q = {"marketplaceIds": "A21TJRUUN4KGV"}
PREVIEW = Q | {"mode": "VALIDATION_PREVIEW"}

Fake = tuple[httpx.AsyncClient, str, Any]


@pytest.fixture
async def fake() -> AsyncIterator[Fake]:
    app = build_fake()
    token = issue_token(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://fake.test"
    ) as client:
        yield client, token, app


def _v(value: Any) -> list[dict[str, Any]]:
    return [{"value": value}]


def _body(**attributes: Any) -> dict[str, Any]:
    return {"productType": "KURTA", "requirements": "LISTING", "attributes": attributes}


def _h(token: str) -> dict[str, str]:
    return {"x-amz-access-token": token}


def _patch(**attributes: Any) -> dict[str, Any]:
    return {
        "productType": "KURTA",
        "patches": [
            {"op": "replace", "path": f"/attributes/{name}", "value": value}
            for name, value in attributes.items()
        ],
    }


async def _scenario(client: httpx.AsyncClient, sku: str, name: str) -> None:
    response = await client.post("/__control", json={"spapi_scenario": {sku: name}})
    assert response.status_code == 200


async def _put(fake: Fake, sku: str, **attributes: Any) -> dict[str, Any]:
    client, token, _ = fake
    response = await client.put(
        f"{BASE}/{sku}", params=Q, json=_body(**attributes), headers=_h(token)
    )
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


async def _get(fake: Fake, sku: str) -> httpx.Response:
    client, token, _ = fake
    return await client.get(f"{BASE}/{sku}", params=Q, headers=_h(token))


def _codes(reply: dict[str, Any]) -> list[tuple[str, list[str]]]:
    return [(i["code"], i.get("attributeNames", [])) for i in reply["issues"]]


# ---- the dry run ----


async def test_preview_never_stores_and_is_counted(fake: Fake) -> None:
    client, token, app = fake
    body = _body(item_name=_v("Kurta"), color=_v("Red"))
    reply = await client.put(f"{BASE}/P-1", params=PREVIEW, json=body, headers=_h(token))
    assert reply.status_code == 200
    assert reply.json() == {"sku": "P-1", "status": "VALID", "issues": []}
    assert "submissionId" not in reply.json()
    assert (await _get(fake, "P-1")).status_code == 404
    assert app.state.fake.spapi_preview_calls == 1
    assert app.state.fake.spapi_items == {}


async def test_preview_invalid_lists_scenario_issue(fake: Fake) -> None:
    client, token, _ = fake
    await _scenario(client, "P-2", "enum_fold")
    body = _body(color=_v("navy"))
    reply = (await client.put(f"{BASE}/P-2", params=PREVIEW, json=body, headers=_h(token))).json()
    assert reply["status"] == "INVALID"
    assert _codes(reply) == [("99300", ["color"])]


@pytest.mark.parametrize("mode", ["FOO", "validation_preview", ""])
async def test_other_mode_values_are_400(fake: Fake, mode: str) -> None:
    client, token, _ = fake
    reply = await client.put(
        f"{BASE}/P-3", params=Q | {"mode": mode}, json=_body(color=_v("Red")), headers=_h(token)
    )
    assert reply.status_code == 400


async def test_preview_on_patch_is_400(fake: Fake) -> None:
    client, token, _ = fake
    await _put(fake, "P-4", color=_v("Red"))
    reply = await client.patch(
        f"{BASE}/P-4", params=PREVIEW, json=_patch(color=_v("Blue")), headers=_h(token)
    )
    assert reply.status_code == 400


async def test_preview_reply_override_once(fake: Fake) -> None:
    client, token, _ = fake
    await client.post("/__control", json={"spapi_preview_reply": {"P-5": "ACCEPTED"}})
    body = _body(color=_v("Red"))
    first = (await client.put(f"{BASE}/P-5", params=PREVIEW, json=body, headers=_h(token))).json()
    again = (await client.put(f"{BASE}/P-5", params=PREVIEW, json=body, headers=_h(token))).json()
    assert (first["status"], again["status"]) == ("ACCEPTED", "VALID")


# ---- value-based scenarios: fail, fixed resend passes, unchanged resend fails ----

# name -> (attribute, the bad value, the fixed value)
VALUE_CASES: dict[str, tuple[str, Any, Any]] = {
    "enum_fold": ("color", _v("navy"), _v("Red")),
    "size_alias": ("size", _v("XXL"), _v("2XL")),
    "title_too_long": ("item_name", _v("K" * 151), _v("Kurta")),
    "identifier_invalid": (
        "externally_assigned_product_identifier",
        [{"type": "ean", "value": "8 901234-567890"}],
        [{"type": "ean", "value": "8901234567890"}],
    ),
}


@pytest.mark.parametrize("name", sorted(VALUE_CASES))
async def test_value_scenario_fix_passes(fake: Fake, name: str) -> None:
    client, _, _ = fake
    attribute, bad, good = VALUE_CASES[name]
    sku = f"V-{name}"
    await _scenario(client, sku, name)
    first = await _put(fake, sku, **{attribute: bad, "brand": _v("Acme")})
    assert first["status"] == "INVALID"  # an INVALID create stores nothing
    assert _codes(first) == [(SCENARIOS[name]["code"], [attribute])]
    assert (await _get(fake, sku)).status_code == 404
    again = await _put(fake, sku, **{attribute: bad, "brand": _v("Acme")})
    assert again["status"] == "INVALID"
    fixed = await _put(fake, sku, **{attribute: good, "brand": _v("Acme")})
    assert (fixed["status"], fixed["issues"]) == ("ACCEPTED", [])
    read = (await _get(fake, sku)).json()
    assert read["summaries"][0]["status"][0] == "BUYABLE" and read["issues"] == []


async def test_enum_fold_on_a_stored_sku_patch(fake: Fake) -> None:
    client, token, app = fake
    await _put(fake, "S-1", color=_v("Red"), item_name=_v("Kurta"))
    await _scenario(client, "S-1", "enum_fold")
    stored = app.state.fake.spapi_items[("A1SELLER", "S-1")]["attributes"]
    stored["color"] = _v("navy")  # the value Amazon later reads as wrong
    assert (await _get(fake, "S-1")).json()["summaries"][0]["status"][0] == "DISCOVERABLE"
    bad = await client.patch(
        f"{BASE}/S-1", params=Q, json=_patch(color=_v("navy")), headers=_h(token)
    )
    assert bad.json()["status"] == "INVALID"
    good = await client.patch(
        f"{BASE}/S-1", params=Q, json=_patch(color=_v("Red")), headers=_h(token)
    )
    assert good.json()["status"] == "ACCEPTED"
    assert (await _get(fake, "S-1")).json()["summaries"][0]["status"][0] == "BUYABLE"


async def test_missing_derivable(fake: Fake) -> None:
    client, _, _ = fake
    await _scenario(client, "M-1", "missing_derivable")
    first = await _put(fake, "M-1", color=_v("Red"))
    assert first["status"] == "INVALID"
    assert _codes(first) == [("90220", ["department"])]
    assert first["issues"][0]["categories"] == ["MISSING_ATTRIBUTE"]
    fixed = await _put(fake, "M-1", color=_v("Red"), department=_v("women"))
    assert fixed["status"] == "ACCEPTED"
    assert (await _get(fake, "M-1")).json()["issues"] == []


async def test_locked_attribute(fake: Fake) -> None:
    client, token, _ = fake
    await _scenario(client, "L-1", "locked_attribute")
    first = await _put(fake, "L-1", item_type_keyword=_v("kurtas"), color=_v("Red"))
    assert first["status"] == "ACCEPTED"  # not stored yet: no lock on a create
    patch = _patch(item_type_keyword=_v("kurta-sets"), color=_v("Blue"))
    locked = (await client.patch(f"{BASE}/L-1", params=Q, json=patch, headers=_h(token))).json()
    assert ("4005033", ["item_type_keyword"]) in _codes(locked)
    without = (
        await client.patch(
            f"{BASE}/L-1", params=Q, json=_patch(color=_v("Blue")), headers=_h(token)
        )
    ).json()
    assert (without["status"], without["issues"]) == ("ACCEPTED", [])


async def test_image_main_refuses_the_first_main(fake: Fake) -> None:
    client, _, _ = fake
    await _scenario(client, "I-1", "image_main")
    first_main = [{"media_location": "https://img.test/a.jpg"}]
    other_main = [{"media_location": "https://img.test/b.jpg"}]
    first = await _put(fake, "I-1", main_product_image_locator=first_main, color=_v("Red"))
    assert first["status"] == "INVALID"
    assert first["issues"][0]["categories"] == ["INVALID_IMAGE"]
    again = await _put(fake, "I-1", main_product_image_locator=first_main, color=_v("Red"))
    assert again["status"] == "INVALID"
    fixed = await _put(fake, "I-1", main_product_image_locator=other_main, color=_v("Red"))
    assert fixed["status"] == "ACCEPTED"


@pytest.mark.parametrize("name", ["brand_gating", "unknown_code"])
async def test_never_clears(fake: Fake, name: str) -> None:
    client, _, _ = fake
    await _scenario(client, f"G-{name}", name)
    for color in ("navy", "Red"):
        reply = await _put(fake, f"G-{name}", color=_v(color))
        assert reply["status"] == "INVALID"
        assert _codes(reply) == [(SCENARIOS[name]["code"], [])]
    if name == "unknown_code":
        assert "categories" not in reply["issues"][0]
    else:
        assert reply["issues"][0]["categories"] == ["QUALIFICATION_REQUIRED"]


async def test_recurring_fails_after_a_fix(fake: Fake) -> None:
    client, _, _ = fake
    await _scenario(client, "R-1", "recurring")
    assert (await _put(fake, "R-1", color=_v("navy")))["status"] == "INVALID"
    assert (await _put(fake, "R-1", color=_v("Red")))["status"] == "INVALID"


async def test_async_after_accept(fake: Fake) -> None:
    client, token, _ = fake
    await _scenario(client, "A-1", "async_after_accept")
    written = await _put(fake, "A-1", color=_v("navy"), item_name=_v("Kurta"))
    assert (written["status"], written["issues"]) == ("ACCEPTED", [])
    read = (await _get(fake, "A-1")).json()
    assert read["summaries"][0]["status"] == ["DISCOVERABLE"]
    assert _codes(read) == [("99300", ["color"])]
    patched = await client.patch(
        f"{BASE}/A-1", params=Q, json=_patch(color=_v("Red")), headers=_h(token)
    )
    assert patched.json()["status"] == "ACCEPTED"
    read = (await _get(fake, "A-1")).json()
    assert read["summaries"][0]["status"][0] == "BUYABLE" and read["issues"] == []


async def test_reset_by_prefix_keeps_another_prefix(fake: Fake) -> None:
    client, _, app = fake
    await _scenario(client, "RUN1-a", "enum_fold")
    await _scenario(client, "RUN2-a", "enum_fold")
    await client.post("/__control", json={"spapi_preview_reply": {"RUN1-a": "ACCEPTED"}})
    await client.post("/__control/reset", json={"prefix": "RUN1-"})
    assert app.state.fake.spapi_scenario == {"RUN2-a": "enum_fold"}
    assert app.state.fake.spapi_preview_reply == {}


async def test_unknown_scenario_name_ignored(fake: Fake) -> None:
    client, _, app = fake
    await _scenario(client, "X-1", "teleport")
    assert app.state.fake.spapi_scenario == {}
