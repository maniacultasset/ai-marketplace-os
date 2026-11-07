"""SI5 tester attacks (round 1) through the real app as app_rw: concurrent redeems, joins,
a factor removed after a reset link is issued, refusals that must stay the generic 404, and
barcode inputs that must be a 422, never a 500. The two creation races it found are regression
tests beside their features: `test_invites.py` (concurrent invites for one email) and
`test_password_reset_links.py` (concurrent reset links for one member)."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD, PREFIX, Conn, World, audit_rows, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .imports_support import csv_bytes, import_fixture, imports_db, row, row_results, run_job
from .jobs_support import JobWorld
from .mfa_support import login, mfa_on

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
# ASCII digits shifted to their full-width forms (U+FF10..U+FF19)
FULL_WIDTH = {ord(d): ord(d) + 0xFEE0 for d in "0123456789"}
__all__ = ["imports_db"]

INVITES = f"{PREFIX}/invites"
RESET = f"{PREFIX}/auth/password/reset"
NEW_PASSWORD = PASSWORD.replace("Test", "Fresh")
OTHER_PASSWORD = PASSWORD.replace("Test", "Other")


@pytest.fixture
def world(auth_db: Conn) -> World:
    return build_world(auth_db)


def _as(world: World, email: str, **kwargs: Any) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug, **kwargs))


def _reset_limits(conn: Conn) -> None:
    from tests.support.rate_limits import reset_rate_limits

    reset_rate_limits(conn)


async def _invite(api: httpx.AsyncClient, world: World, email: str) -> httpx.Response:
    return await api.post(
        INVITES, json={"email": email, "role": "reviewer"}, headers=_as(world, "owner@acme.test")
    )


async def _invite_token(api: httpx.AsyncClient, world: World, email: str) -> str:
    response = await _invite(api, world, email)
    assert response.status_code == 201, response.text
    return str(response.json()["invite_url"]).split("#token=", 1)[1]


# ---- invites ----


async def test_join_adds_a_membership_only_in_the_inviting_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """A bharat owner joins acme as a reviewer: the bharat role is untouched, no third row."""
    token = await _invite_token(api, world, "owner@bharat.test")
    joined = await api.post(
        f"{INVITES}/join", json={"token": token}, headers=_as(world, "owner@bharat.test")
    )
    assert joined.status_code == 200, joined.text
    assert joined.json() == {"tenant_id": str(world.tenant("acme")), "role": "reviewer"}
    rows = auth_db.execute(
        "SELECT tenant_id, role FROM tenant_memberships WHERE user_id = %s ORDER BY role",
        [world.user("owner@bharat.test")],
    ).fetchall()
    assert sorted(rows) == sorted(
        [(world.tenant("bharat"), "owner"), (world.tenant("acme"), "reviewer")]
    )


async def test_join_with_a_role_claim_in_the_token_keeps_the_invited_role(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The caller's JWT says owner in bharat; the acme membership is the invite's role."""
    token = await _invite_token(api, world, "operator@bharat.test")
    joined = await api.post(
        f"{INVITES}/join",
        json={"token": token},
        headers=_as(world, "operator@bharat.test", role="owner"),
    )
    assert joined.status_code == 200, joined.text
    (role,) = auth_db.execute(
        "SELECT role FROM tenant_memberships WHERE user_id = %s AND tenant_id = %s",
        [world.user("operator@bharat.test"), world.tenant("acme")],
    ).fetchone() or (None,)
    assert role == "reviewer"


# ---- reset links ----


@pytest.fixture
def reset_world(auth_db: Conn, monkeypatch: pytest.MonkeyPatch) -> World:
    mfa_on(monkeypatch)
    built = build_world(auth_db)
    auth_db.execute(
        "INSERT INTO user_mfa_factors (user_id, status, secret_blob, key_version, confirmed_at) "
        "VALUES (%s, 'active', %s, 'local-v1', now())",
        [built.user("reviewer@acme.test"), b"x" * 40],
    )
    return built


async def _reset_token(api: httpx.AsyncClient, world: World) -> str:
    response = await api.post(
        f"{PREFIX}/members/{world.user('reviewer@acme.test')}/password-reset",
        headers=_as(world, "owner@acme.test", amr=["pwd", "otp"], mfa_at=int(time.time())),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["reset_url"]).split("#token=", 1)[1]


async def test_a_factor_removed_after_issue_kills_the_link(
    api: httpx.AsyncClient, reset_world: World, auth_db: Conn
) -> None:
    """D8 is re-checked at redeem: no confirmed TOTP any more, the generic 404."""
    token = await _reset_token(api, reset_world)
    auth_db.execute(
        "DELETE FROM user_mfa_factors WHERE user_id = %s", [reset_world.user("reviewer@acme.test")]
    )
    response = await api.post(RESET, json={"token": token, "new_password": NEW_PASSWORD})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["type"] == "NOT_FOUND"


async def test_a_target_made_platform_admin_after_issue_is_404(
    api: httpx.AsyncClient, reset_world: World, auth_db: Conn
) -> None:
    token = await _reset_token(api, reset_world)
    auth_db.execute(
        "UPDATE users SET is_platform_admin = true WHERE id = %s",
        [reset_world.user("reviewer@acme.test")],
    )
    response = await api.post(RESET, json={"token": token, "new_password": NEW_PASSWORD})
    assert response.status_code == 404, response.text


async def test_two_concurrent_redeems_set_one_password(
    api: httpx.AsyncClient, reset_world: World, auth_db: Conn
) -> None:
    token = await _reset_token(api, reset_world)
    first, second = await asyncio.gather(
        api.post(RESET, json={"token": token, "new_password": NEW_PASSWORD}),
        api.post(RESET, json={"token": token, "new_password": OTHER_PASSWORD}),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [204, 404], (first.text, second.text)
    winner = NEW_PASSWORD if first.status_code == 204 else OTHER_PASSWORD
    loser = OTHER_PASSWORD if winner == NEW_PASSWORD else NEW_PASSWORD
    assert len(audit_rows(auth_db, "user.password_reset")) == 1
    used = auth_db.execute(
        "SELECT count(*) FROM password_reset_links WHERE used_at IS NOT NULL AND user_id = %s",
        [reset_world.user("reviewer@acme.test")],
    ).fetchone()
    assert used == (1,)
    _reset_limits(auth_db)
    assert (await login(api, "reviewer@acme.test", loser)).status_code == 401
    assert (await login(api, "reviewer@acme.test", winner)).status_code == 200


async def test_a_weak_password_on_a_used_link_is_the_generic_404(
    api: httpx.AsyncClient, reset_world: World
) -> None:
    """A spent link never answers with the password rules (it would say the link was real)."""
    token = await _reset_token(api, reset_world)
    assert (
        await api.post(RESET, json={"token": token, "new_password": NEW_PASSWORD})
    ).status_code == 204
    weak = await api.post(RESET, json={"token": token, "new_password": "Reviewer-Pass-0001"})
    assert weak.status_code == 404, weak.text


# ---- barcodes over the API: bad input is a 422, never a 500 ----


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "ean", "value": "٤٠٠٦٣٨١٣٣٣٩٣١"},  # Arabic-Indic digits
        {"type": "ean", "value": "4006381333931".translate(FULL_WIDTH)},
        {"type": "ean", "value": "4006381333931\x00"},
        {"type": "ean", "value": "40063813339310"},
        {"type": "ean", "value": ""},
        {"type": "ean", "value": "9" * 5000},
        {"type": "EAN", "value": "4006381333931"},
        {"type": "isbn", "value": "4006381333931"},
        {"type": "ean", "value": 4006381333931},
        {"type": "ean"},
        {"value": "4006381333931"},
        {"type": "ean", "value": "4006381333931", "extra": 1},
    ],
)
async def test_malformed_barcodes_are_422(
    api: httpx.AsyncClient, products_world: World, bad: dict[str, Any]
) -> None:
    from .products_support import PRODUCTS, bearer_for, site_body

    response = await api.post(
        PRODUCTS,
        json=site_body(f"SI5-T-{uuid.uuid4().hex[:8]}", product_identifier=bad),
        headers=bearer_for(products_world, "operator@acme.test"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] in ("VALIDATION_FAILED",)


async def test_a_upc_and_the_same_digits_as_an_ean13_are_one_barcode(
    api: httpx.AsyncClient, products_world: World
) -> None:
    """UPC 036000291452 is EAN-13 0036000291452 (lpad 14 in `products_identifier_uq`)."""
    from .products_support import PRODUCTS, bearer_for, site_body

    headers = bearer_for(products_world, "operator@acme.test")
    upc = {"type": "upc", "value": "036000291452"}
    ean = {"type": "ean", "value": "0036000291452"}
    first = await api.post(
        PRODUCTS, json=site_body("SI5-T-UPC", product_identifier=upc), headers=headers
    )
    assert first.status_code == 201, first.text
    second = await api.post(
        PRODUCTS, json=site_body("SI5-T-EAN", product_identifier=ean), headers=headers
    )
    assert second.status_code == 422, second.text
    loc = second.json()["error"]["details"][0]["loc"]
    assert loc == ["body", "product_identifier", "value"]


async def test_a_null_exemption_on_patch_is_422(
    api: httpx.AsyncClient, products_world: World
) -> None:
    from .products_support import PRODUCTS, bearer_for, site_body

    headers = bearer_for(products_world, "operator@acme.test")
    created = await api.post(PRODUCTS, json=site_body("SI5-T-NULL"), headers=headers)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    etag = (await api.get(f"{PRODUCTS}/{pid}", headers=headers)).headers["ETag"]
    patched = await api.patch(
        f"{PRODUCTS}/{pid}",
        json={"identifier_exemption": None},
        headers={**headers, "If-Match": etag},
    )
    assert patched.status_code == 422, patched.text


# ---- CSV barcodes: a duplicate in one file, a BOM and an upper-case header ----


def _stored(jw: JobWorld, sku: str) -> tuple[Any, ...]:
    found = jw.conn.execute(
        "SELECT identifier_type, identifier_value, identifier_status FROM products WHERE sku = %s",
        [sku],
    ).fetchone()
    return tuple(found) if found is not None else ()


async def test_one_barcode_on_two_csv_rows_fails_only_the_second(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    import_id, job_id = await import_fixture(
        imports_db,
        api,
        csv_bytes([row(50, ean="4006381333931"), row(51, barcode="4006381333931"), row(52)]),
    )
    await run_job(job_id)
    statuses = {r[2]: r[1] for r in row_results(imports_db.conn, import_id)}
    assert statuses == {
        "SKU-00050": "imported",
        "SKU-00051": "failed",
        "SKU-00052": "imported",
    }, statuses
    assert _stored(imports_db, "SKU-00050") == ("ean", "4006381333931", "provided")


async def test_a_bom_and_upper_case_barcode_header_import(
    imports_db: JobWorld, api: httpx.AsyncClient
) -> None:
    body = csv_bytes([row(60, EAN="4006381333931"), row(61, EAN="")])
    data = b"\xef\xbb\xbf" + body
    import_id, job_id = await import_fixture(imports_db, api, data)
    await run_job(job_id)
    statuses = {r[2]: r[1] for r in row_results(imports_db.conn, import_id)}
    assert statuses == {"SKU-00060": "imported", "SKU-00061": "imported"}, statuses
    assert _stored(imports_db, "SKU-00060") == ("ean", "4006381333931", "provided")
    assert _stored(imports_db, "SKU-00061") == (None, None, None)
