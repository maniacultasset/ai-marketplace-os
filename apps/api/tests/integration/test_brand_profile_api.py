"""SI1 T13 (AC6, AC7, AC18): `GET`/`PATCH /api/v1/brand-profile` as app_rw through the real app.
Run in the Docker batch (`make api-tests-integration`)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from .auth_support import World, audit_rows
from .conftest import LOCAL_HOSTS
from .products_support import PRODUCTS, Conn, bad_bearer, bearer_for, site_body

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
URL = "/api/v1/brand-profile"
PARTY: dict[str, Any] = {
    "legal_name": "Brand Default Pvt Ltd",
    "address_line1": "1 Residency Road",
    "city": "Bengaluru",
    "state_code": "KA",
    "postal_code": "560025",
    "gstin": "29ABCDE1234F1Z5",
    "email": "legal@brand.example",
}


@pytest.fixture
def fresh(products_world: World, auth_db: Conn) -> World:
    """No brand profile row for acme (a tenant may lack it)."""
    auth_db.execute(
        "DELETE FROM brand_profiles WHERE tenant_id = %s", [products_world.tenant("acme")]
    )
    return products_world


def _as(world: World, email: str = "owner@acme.test") -> dict[str, str]:
    return bearer_for(world, email)


async def _etag(api: httpx.AsyncClient, world: World, email: str = "owner@acme.test") -> str:
    response = await api.get(URL, headers=_as(world, email))
    assert response.status_code == 200, response.text
    return str(response.headers["ETag"])


async def _put(
    api: httpx.AsyncClient,
    world: World,
    body: object,
    *,
    etag: str | None = None,
    email: str = "owner@acme.test",
) -> httpx.Response:
    tag = etag if etag is not None else await _etag(api, world, email)
    return await api.patch(URL, json=body, headers={**_as(world, email), "If-Match": tag})


def _audits(conn: Conn) -> list[tuple[Any, ...]]:
    return audit_rows(conn, "brand_profile.updated")


# ---- GET ----


@pytest.mark.endpoint("brand_profile_get", case="success")
async def test_get_without_row_is_empty(api: httpx.AsyncClient, fresh: World) -> None:
    response = await api.get(URL, headers=_as(fresh, "reviewer@acme.test"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "default_manufacturer": None,
        "website_url": None,
        "brand_description": None,
        "brand_voice": None,
        "updated_at": None,
    }
    assert response.headers["ETag"]


@pytest.mark.endpoint("brand_profile_get", case="missing_auth")
async def test_get_missing_auth(api: httpx.AsyncClient, fresh: World) -> None:
    response = await api.get(URL)
    assert response.status_code == 401


@pytest.mark.endpoint("brand_profile_get", case="invalid_token", variant="expired")
@pytest.mark.endpoint("brand_profile_get", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("brand_profile_get", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_get_invalid_token(api: httpx.AsyncClient, fresh: World, variant: str) -> None:
    response = await api.get(URL, headers=bad_bearer(fresh, variant))
    assert response.status_code == 401


@pytest.mark.endpoint("brand_profile_get", case="wrong_tenant")
async def test_get_reads_only_the_tid_row(api: httpx.AsyncClient, fresh: World) -> None:
    stored = await _put(api, fresh, {"default_manufacturer": PARTY})
    assert stored.status_code == 200, stored.text
    theirs = await api.get(URL, headers=_as(fresh, "owner@bharat.test"))
    assert theirs.status_code == 200
    assert PARTY["legal_name"] not in theirs.text


# ---- PUT ----


@pytest.mark.endpoint("brand_profile_update", case="success")
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_first_put_with_none_etag(api: httpx.AsyncClient, fresh: World, role: str) -> None:
    response = await _put(api, fresh, {"default_manufacturer": PARTY}, email=f"{role}@acme.test")
    assert response.status_code == 200, response.text
    out = response.json()["default_manufacturer"]
    assert out["legal_name"] == PARTY["legal_name"] and out["city"] == "Bengaluru"
    assert "email" not in out
    assert response.headers["ETag"]


@pytest.mark.endpoint("brand_profile_update", case="validation")
@pytest.mark.parametrize(
    "body",
    [
        {"default_manufacturer": {**PARTY, "state_code": "XX"}},
        {"default_manufacturer": {**PARTY, "postal_code": "012345"}},
        {"other": 1},
        # client item 1: the sign-up form's website rule, cut to the column's 2000
        {"website_url": "kalakar.example"},
        {"website_url": "ftp://kalakar.example"},
        {"website_url": "https://kalakar example.in"},
        {"website_url": "https://localhost"},
        {"website_url": "https://kalakar.example/" + "a" * 1980},
        {"website_url": "https://kalakar.example/\u0000"},
        {"website_url": 7},
        {"brand_description": "x" * 2001},
        {"brand_voice": {"price_segment": "cheap"}},
        {"brand_voice": {"words_to_avoid": ["x" * 61]}},
        {"brand_voice": {"example_copy": "Bearer abcdefghijklmnopqrstu"}},
        {"brand_voice": {"tone": "warm"}},
    ],
)
async def test_put_validation(api: httpx.AsyncClient, fresh: World, body: dict[str, Any]) -> None:
    response = await _put(api, fresh, body)
    assert response.status_code == 422


@pytest.mark.endpoint("brand_profile_update", case="missing_auth")
async def test_put_missing_auth(api: httpx.AsyncClient, fresh: World) -> None:
    response = await api.patch(
        URL, json={"default_manufacturer": None}, headers={"If-Match": '"' + "0" * 36 + '"'}
    )
    assert response.status_code == 401


@pytest.mark.endpoint("brand_profile_update", case="invalid_token", variant="expired")
@pytest.mark.endpoint("brand_profile_update", case="invalid_token", variant="bad_signature")
@pytest.mark.endpoint("brand_profile_update", case="invalid_token", variant="wrong_typ")
@pytest.mark.parametrize("variant", ["expired", "bad_signature", "wrong_typ"])
async def test_put_invalid_token(api: httpx.AsyncClient, fresh: World, variant: str) -> None:
    response = await api.patch(
        URL,
        json={"default_manufacturer": None},
        headers={**bad_bearer(fresh, variant, "owner@acme.test"), "If-Match": '"' + "0" * 36 + '"'},
    )
    assert response.status_code == 401


@pytest.mark.endpoint("brand_profile_update", case="wrong_role")
@pytest.mark.parametrize("role", ["operator", "reviewer"])
async def test_put_wrong_role(api: httpx.AsyncClient, fresh: World, role: str) -> None:
    tag = await _etag(api, fresh)
    response = await _put(
        api, fresh, {"default_manufacturer": PARTY}, etag=tag, email=f"{role}@acme.test"
    )
    assert response.status_code == 403


@pytest.mark.endpoint("brand_profile_update", case="wrong_tenant")
async def test_put_with_other_tenant_etag_409(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    await _put(api, fresh, {"default_manufacturer": PARTY}, email="owner@bharat.test")
    theirs = await _etag(api, fresh, "owner@bharat.test")
    response = await _put(api, fresh, {"default_manufacturer": PARTY}, etag=theirs)
    assert response.status_code == 409
    row = auth_db.execute(
        "SELECT default_manufacturer_party_id FROM brand_profiles WHERE tenant_id = %s",
        [fresh.tenant("acme")],
    ).fetchone()
    assert row is None or row[0] is None


@pytest.mark.endpoint("brand_profile_update", case="conflict")
async def test_put_stale_etag_409(api: httpx.AsyncClient, fresh: World) -> None:
    old = await _etag(api, fresh)
    assert (await _put(api, fresh, {"default_manufacturer": PARTY}, etag=old)).status_code == 200
    moved = {**PARTY, "legal_name": "Another Maker Pvt Ltd"}
    response = await _put(api, fresh, {"default_manufacturer": moved}, etag=old)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "STALE_VERSION"


async def test_first_put_with_made_up_tag_409(api: httpx.AsyncClient, fresh: World) -> None:
    response = await _put(
        api,
        fresh,
        {"default_manufacturer": PARTY},
        etag='"' + "1" * 8 + "-1111-1111-1111-" + "1" * 12 + '"',
    )
    assert response.status_code == 409


async def test_concurrent_first_puts_one_wins(api: httpx.AsyncClient, fresh: World) -> None:
    tag = await _etag(api, fresh)
    other = {**PARTY, "legal_name": "Race Maker Pvt Ltd"}
    results = await asyncio.gather(
        _put(api, fresh, {"default_manufacturer": PARTY}, etag=tag),
        _put(api, fresh, {"default_manufacturer": other}, etag=tag),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_same_party_again_is_a_no_op(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    first = await _put(api, fresh, {"default_manufacturer": PARTY})
    assert first.status_code == 200
    audits = len(_audits(auth_db))
    again = await _put(api, fresh, {"default_manufacturer": PARTY}, etag=first.headers["ETag"])
    assert again.status_code == 200
    assert again.json()["default_manufacturer"]["id"] == first.json()["default_manufacturer"]["id"]
    assert again.headers["ETag"] == first.headers["ETag"]
    assert len(_audits(auth_db)) == audits


async def test_null_clears(api: httpx.AsyncClient, fresh: World) -> None:
    first = await _put(api, fresh, {"default_manufacturer": PARTY})
    cleared = await _put(api, fresh, {"default_manufacturer": None}, etag=first.headers["ETag"])
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["default_manufacturer"] is None


async def test_audit_after_has_no_party_data(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    assert (await _put(api, fresh, {"default_manufacturer": PARTY})).status_code == 200
    after = _audits(auth_db)[-1][7]
    text = json.dumps(after)
    assert set(after) == {"provenance"}
    assert "@" not in text and PARTY["legal_name"] not in text and "Residency" not in text


async def test_products_inherit_and_patch_returns_to_it(
    api: httpx.AsyncClient, fresh: World
) -> None:
    """AC7: no override reads the brand party as inherited; PATCH `manufacturer: null` returns
    an overridden product to it."""
    assert (await _put(api, fresh, {"default_manufacturer": PARTY})).status_code == 200
    operator = bearer_for(fresh, "operator@acme.test")
    plain = await api.post(PRODUCTS, json=site_body("SI1-BP-1"), headers=operator)
    assert plain.status_code == 201, plain.text
    got = await api.get(f"{PRODUCTS}/{plain.json()['id']}", headers=operator)
    assert got.json()["manufacturer"]["legal_name"] == PARTY["legal_name"]
    assert got.json()["manufacturer"]["inherited"] is True
    own = {**PARTY, "legal_name": "Own Maker Pvt Ltd"}
    overridden = await api.post(
        PRODUCTS, json=site_body("SI1-BP-2", manufacturer=own), headers=operator
    )
    assert overridden.json()["manufacturer"]["inherited"] is False
    back = await api.patch(
        f"{PRODUCTS}/{overridden.json()['id']}",
        json={"manufacturer": None},
        headers={**operator, "If-Match": overridden.headers["ETag"]},
    )
    assert back.status_code == 200, back.text
    assert back.json()["manufacturer"]["legal_name"] == PARTY["legal_name"]
    assert back.json()["manufacturer"]["inherited"] is True


# ---- client item 1: the brand website ----

SITE = "https://kalakar.example/shop"


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_website_set_read_and_cleared(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn, role: str
) -> None:
    email = f"{role}@acme.test"
    stored = await _put(api, fresh, {"website_url": SITE}, email=email)
    assert stored.status_code == 200, stored.text
    assert stored.json()["website_url"] == SITE
    assert stored.json()["default_manufacturer"] is None
    read = await api.get(URL, headers=_as(fresh, "reviewer@acme.test"))
    assert read.json()["website_url"] == SITE
    assert read.headers["ETag"] == stored.headers["ETag"]
    row = auth_db.execute(
        "SELECT website_url, provenance ? 'website_url' FROM brand_profiles WHERE tenant_id = %s",
        [fresh.tenant("acme")],
    ).fetchone()
    assert row == (SITE, False)
    cleared = await _put(
        api, fresh, {"website_url": None}, etag=stored.headers["ETag"], email=email
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["website_url"] is None
    row = auth_db.execute(
        "SELECT website_url, provenance ? 'website_url' FROM brand_profiles WHERE tenant_id = %s",
        [fresh.tenant("acme")],
    ).fetchone()
    assert row == (None, False)


async def test_website_and_manufacturer_are_independent(
    api: httpx.AsyncClient, fresh: World
) -> None:
    first = await _put(api, fresh, {"default_manufacturer": PARTY})
    site = await _put(api, fresh, {"website_url": SITE}, etag=first.headers["ETag"])
    assert site.status_code == 200, site.text
    assert site.json()["default_manufacturer"]["legal_name"] == PARTY["legal_name"]
    party = await _put(api, fresh, {"default_manufacturer": None}, etag=site.headers["ETag"])
    assert party.json()["website_url"] == SITE and party.json()["default_manufacturer"] is None


async def test_same_website_again_is_a_no_op(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    first = await _put(api, fresh, {"website_url": SITE})
    audits = len(_audits(auth_db))
    again = await _put(api, fresh, {"website_url": SITE}, etag=first.headers["ETag"])
    assert again.status_code == 200
    assert again.headers["ETag"] == first.headers["ETag"]
    assert len(_audits(auth_db)) == audits


# ---- client feedback item 19: the brand description and the brand voice ----

VOICE: dict[str, Any] = {
    "audience": "Women 25 to 40 who buy for office wear",
    "price_segment": "mid",
    "positioning": "Everyday handloom, made to last",
    "differentiators": ["Hand-block printed"],
    "words_to_use": ["breathable", "easy care"],
    "words_to_avoid": ["cheap"],
    "claim_limits": ["No organic claims"],
    "example_copy": "Soft cotton that keeps its shape.",
}


def _stored(conn: Conn, world: World) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT brand_description, brand_voice, provenance FROM brand_profiles "
        "WHERE tenant_id = %s",
        [world.tenant("acme")],
    ).fetchone()


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_voice_and_description_round_trip(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn, role: str
) -> None:
    body = {"brand_description": "Cotton wear from Jaipur.", "brand_voice": VOICE}
    saved = await _put(api, fresh, body, email=f"{role}@acme.test")
    assert saved.status_code == 200, saved.text
    assert saved.json()["brand_voice"] == VOICE
    assert saved.json()["brand_description"] == "Cotton wear from Jaipur."
    read = await api.get(URL, headers=_as(fresh, "reviewer@acme.test"))
    assert read.json()["brand_voice"] == VOICE and read.headers["ETag"] == saved.headers["ETag"]
    row = _stored(auth_db, fresh)
    assert row is not None and row[1] == VOICE
    assert row[2]["brand_description"]["source"] == "seller"
    assert "value" not in row[2]["brand_description"]


async def test_voice_keeps_the_manufacturer_and_vice_versa(
    api: httpx.AsyncClient, fresh: World
) -> None:
    first = await _put(api, fresh, {"default_manufacturer": PARTY})
    voiced = await _put(api, fresh, {"brand_voice": VOICE}, etag=first.headers["ETag"])
    assert voiced.status_code == 200, voiced.text
    assert voiced.json()["default_manufacturer"]["legal_name"] == PARTY["legal_name"]
    moved = {**PARTY, "legal_name": "Another Maker Pvt Ltd"}
    again = await _put(api, fresh, {"default_manufacturer": moved}, etag=voiced.headers["ETag"])
    assert again.json()["brand_voice"] == VOICE


async def test_voice_same_again_is_a_no_op(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    first = await _put(api, fresh, {"brand_voice": VOICE, "brand_description": "Cotton."})
    audits = len(_audits(auth_db))
    # any key order, trimmed text and a repeated word store the same voice
    same = {**dict(reversed(list(VOICE.items()))), "words_to_avoid": [" cheap", "Cheap"]}
    again = await _put(
        api,
        fresh,
        {"brand_voice": same, "brand_description": "Cotton."},
        etag='"' + "1" * 8 + "-1111-1111-1111-" + "1" * 12 + '"',  # well formed, stale
    )
    assert again.status_code == 200, again.text
    assert again.headers["ETag"] == first.headers["ETag"]
    assert len(_audits(auth_db)) == audits


async def test_website_stale_etag_409_and_wrong_role_403(
    api: httpx.AsyncClient, fresh: World
) -> None:
    old = await _etag(api, fresh)
    assert (await _put(api, fresh, {"website_url": SITE}, etag=old)).status_code == 200
    stale = await _put(api, fresh, {"website_url": "https://other.example"}, etag=old)
    assert stale.status_code == 409
    denied = await _put(
        api, fresh, {"website_url": "https://other.example"}, email="operator@acme.test"
    )
    assert denied.status_code == 403


async def test_website_audit_after_is_value_less(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    assert (await _put(api, fresh, {"website_url": SITE})).status_code == 200
    after = _audits(auth_db)[-1][7]
    assert set(after) == {"provenance"} and set(after["provenance"]) == {"website_url"}
    assert "kalakar" not in json.dumps(after)


async def test_voice_stale_etag_409_writes_nothing(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    old = await _etag(api, fresh)
    assert (await _put(api, fresh, {"brand_voice": VOICE}, etag=old)).status_code == 200
    changed = {**VOICE, "audience": "Students"}
    response = await _put(api, fresh, {"brand_voice": changed}, etag=old)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "STALE_VERSION"
    row = _stored(auth_db, fresh)
    assert row is not None and row[1]["audience"] == VOICE["audience"]


async def test_voice_wrong_role_403(api: httpx.AsyncClient, fresh: World) -> None:
    tag = await _etag(api, fresh)
    for role in ("operator", "reviewer"):
        response = await _put(
            api, fresh, {"brand_voice": VOICE}, etag=tag, email=f"{role}@acme.test"
        )
        assert response.status_code == 403


async def test_voice_clears_with_null_or_all_empty(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    first = await _put(api, fresh, {"brand_voice": VOICE, "brand_description": "Cotton."})
    empty = await _put(
        api,
        fresh,
        {"brand_voice": {"words_to_use": [], "audience": None}, "brand_description": None},
        etag=first.headers["ETag"],
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["brand_voice"] is None and empty.json()["brand_description"] is None
    row = _stored(auth_db, fresh)
    assert row is not None and row[0] is None and row[1] is None
    assert "brand_description" not in row[2]
    again = await _put(api, fresh, {"brand_voice": VOICE}, etag=empty.headers["ETag"])
    cleared = await _put(api, fresh, {"brand_voice": None}, etag=again.headers["ETag"])
    assert cleared.json()["brand_voice"] is None


async def test_voice_audit_names_fields_never_text(
    api: httpx.AsyncClient, fresh: World, auth_db: Conn
) -> None:
    first = await _put(api, fresh, {"brand_voice": VOICE, "brand_description": "Cotton."})
    after = _audits(auth_db)[-1][7]
    assert set(after) == {"provenance", "brand_voice"}
    assert after["brand_voice"] == list(VOICE)
    assert set(after["provenance"]) == {"brand_description"}
    text = json.dumps(after)
    assert "Cotton" not in text and "breathable" not in text and "Women" not in text
    moved = {**VOICE, "words_to_avoid": ["cheap", "flimsy"]}
    await _put(api, fresh, {"brand_voice": moved}, etag=first.headers["ETag"])
    assert _audits(auth_db)[-1][7] == {"provenance": {}, "brand_voice": ["words_to_avoid"]}


async def test_voice_is_tenant_scoped(api: httpx.AsyncClient, fresh: World) -> None:
    assert (await _put(api, fresh, {"brand_voice": VOICE})).status_code == 200
    theirs = await api.get(URL, headers=_as(fresh, "owner@bharat.test"))
    assert theirs.status_code == 200
    assert "breathable" not in theirs.text
