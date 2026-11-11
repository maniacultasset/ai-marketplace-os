"""ADR-0019 amendment (11 Nov 2026): the sign-up status handle and `POST /signup/status/poll`,
through the real app as app_rw under FORCE RLS.

Every answer that observes a pending request (the form, the status read, the Google `pending`
outcome and the Google sign-up) carries a fresh handle; only its sha256 and a 30-day expiry are
stored, and a newer handle replaces the older one. The poll answers only the status, the brand
name, the request and decision times and the reject reason; an unknown, replaced or expired
handle gets one 404. The handle travels in the body only and never reaches a log line.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from .auth_support import PASSWORD as WORLD_PASSWORD
from .auth_support import PREFIX, Conn, World, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS
from .google_support import SIGNUP_GOOGLE, GoogleBrowser, GoogleWorld

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

SIGNUP = f"{PREFIX}/signup"
STATUS = f"{PREFIX}/signup/status"
POLL = f"{PREFIX}/signup/status/poll"
QUEUE = f"{PREFIX}/admin/signup-requests"
ADMIN = "operator@acme.test"
PASSWORD = "Quiet-Harbour-Lamp-7305"
FORM: dict[str, Any] = {
    "brand_name": "Kalakar Weaves",
    "owner_name": "Meera Iyer",
    "email": "founder@kalakar.test",
    "password": PASSWORD,
}
POLL_FIELDS = {"status", "brand_name", "requested_at", "decided_at", "reason"}
HANDLE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.fixture
def world(auth_db: Conn) -> World:
    world = build_world(auth_db)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    return world


def _admin(world: World) -> dict[str, str]:
    return bearer(tokens_for(world, ADMIN, "acme"))


def _digest(handle: str) -> bytes:
    return hashlib.sha256(handle.encode("ascii")).digest()


def _stored(conn: Conn, email: str = FORM["email"]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT r.id, r.status, r.status_handle_hash, "
        "r.status_handle_expires_at - now() FROM signup_requests r "
        "JOIN users u ON u.id = r.user_id WHERE u.email = %s ORDER BY r.created_at",
        [email],
    ).fetchall()


def _well_formed(handle: Any) -> bool:
    return isinstance(handle, str) and len(handle) == 43 and set(handle) <= HANDLE_CHARS


async def _signup(api: httpx.AsyncClient, **changes: Any) -> dict[str, Any]:
    response = await api.post(SIGNUP, json={**FORM, **changes})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def _poll(api: httpx.AsyncClient, handle: str) -> httpx.Response:
    return await api.post(POLL, json={"status_handle": handle})


def _without_ids(value: Any) -> Any:
    """An error body without the per-request correlation id, to compare two refusals."""
    if isinstance(value, dict):
        return {
            k: _without_ids(v)
            for k, v in value.items()
            if k not in {"request_id", "correlation_id"}
        }
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


# ---- issuing: the password form and the status read ----


@pytest.mark.endpoint("signup_status_poll", case="success")
async def test_signup_issues_a_handle_the_poll_reads(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api)
    handle = body["status_handle"]
    assert _well_formed(handle)
    response = await _poll(api, handle)
    assert response.status_code == 200, response.text
    out = response.json()
    assert set(out) == POLL_FIELDS
    assert out["status"] == "pending" and out["brand_name"] == "Kalakar Weaves"
    assert out["decided_at"] is None and out["reason"] is None
    [(request_id, status, stored, ttl)] = _stored(auth_db)
    assert str(request_id) == body["request_id"] and status == "pending"
    # only the sha256 is kept, with a 30-day expiry set by the database
    assert bytes(stored) == _digest(handle) and handle.encode() not in bytes(stored)
    assert 29 * 86400 < ttl.total_seconds() <= 30 * 86400


async def test_poll_reveals_no_id_email_or_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api, phone="+91 98765 43210", website="https://kalakar.test")
    text = (await _poll(api, body["status_handle"])).text
    user_id = auth_db.execute("SELECT id FROM users WHERE email = %s", [FORM["email"]]).fetchone()
    assert user_id is not None
    for leak in (FORM["email"], "Meera Iyer", "98765", "kalakar.test", body["request_id"]):
        assert leak not in text, leak
    assert str(user_id[0]) not in text


async def test_status_read_issues_a_new_handle_and_replaces_the_old(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = (await _signup(api))["status_handle"]
    response = await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})
    assert response.status_code == 200, response.text
    second = response.json()["status_handle"]
    assert _well_formed(second) and second != first
    [(_, _, stored, _)] = _stored(auth_db)
    assert bytes(stored) == _digest(second)
    assert (await _poll(api, first)).status_code == 404
    assert (await _poll(api, second)).json()["status"] == "pending"


async def test_resubmit_while_pending_reissues_for_the_same_request(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    first = await _signup(api)
    again = await _signup(api, brand_name="Another Brand")
    assert again["status_handle"] != first["status_handle"]
    assert len(_stored(auth_db)) == 1
    out = (await _poll(api, again["status_handle"])).json()
    assert out["brand_name"] == "Kalakar Weaves"  # the pending request, not the new form
    assert (await _poll(api, first["status_handle"])).status_code == 404


async def test_wrong_password_gets_a_handle_that_names_nothing(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    await _signup(api)
    blank = await _signup(api, password="Not-the-Password-0123")
    # the same shape as a real answer, and it names nothing: never the real request's handle
    assert set(blank) == {"request_id", "status", "status_handle"}
    assert _well_formed(blank["status_handle"])
    assert (await _poll(api, blank["status_handle"])).status_code == 404
    [(_, _, stored, _)] = _stored(auth_db)
    assert bytes(stored) != _digest(blank["status_handle"])


async def test_existing_member_right_password_gets_a_live_handle(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(
        api, email="owner@acme.test", password=WORLD_PASSWORD, brand_name="Acme Two"
    )
    out = (await _poll(api, body["status_handle"])).json()
    assert out["status"] == "pending" and out["brand_name"] == "Acme Two"


# ---- decisions ----


async def test_poll_follows_approval(api: httpx.AsyncClient, world: World) -> None:
    body = await _signup(api)
    response = await api.post(
        f"{QUEUE}/{body['request_id']}/approve", json={}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    out = (await _poll(api, body["status_handle"])).json()
    assert out["status"] == "approved" and out["decided_at"] is not None
    assert out["reason"] is None and set(out) == POLL_FIELDS
    assert response.json()["tenant_id"] not in str(out)


async def test_poll_shows_the_reject_reason(api: httpx.AsyncClient, world: World) -> None:
    body = await _signup(api)
    reason = "We could not verify the brand; write to support with your GSTIN."
    response = await api.post(
        f"{QUEUE}/{body['request_id']}/reject", json={"reason": reason}, headers=_admin(world)
    )
    assert response.status_code == 200, response.text
    out = (await _poll(api, body["status_handle"])).json()
    assert out["status"] == "rejected" and out["reason"] == reason


async def test_status_read_of_a_decided_request_issues_no_handle(
    api: httpx.AsyncClient, world: World
) -> None:
    body = await _signup(api)
    await api.post(
        f"{QUEUE}/{body['request_id']}/reject", json={"reason": "No."}, headers=_admin(world)
    )
    out = (await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})).json()
    assert out["status"] == "rejected" and out["status_handle"] is None
    # the handle issued while pending still reads the decision
    assert (await _poll(api, body["status_handle"])).json()["status"] == "rejected"


# ---- refusals: one 404 ----


async def test_unknown_and_expired_handles_get_the_same_404(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api)
    replaced = body["status_handle"]
    live = (await api.post(STATUS, json={"email": FORM["email"], "password": PASSWORD})).json()[
        "status_handle"
    ]
    expired = (await _signup(api, email="late@kalakar.test"))["status_handle"]
    auth_db.execute(
        "UPDATE signup_requests SET status_handle_expires_at = now() - interval '1 second' "
        "WHERE status_handle_hash = %s",
        [_digest(expired)],
    )
    unknown = "A" * 43
    refusals = [await _poll(api, h) for h in (unknown, replaced, expired)]
    for response in refusals:
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "NOT_FOUND"
    shapes = [_without_ids(r.json()) for r in refusals]
    assert shapes[0] == shapes[1] == shapes[2]
    assert (await _poll(api, live)).status_code == 200


@pytest.mark.endpoint("signup_status_poll", case="validation")
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status_handle": ""},
        {"status_handle": "short"},
        {"status_handle": "A" * 44},
        {"status_handle": "+" * 43},
        {"status_handle": "A" * 43, "email": "founder@kalakar.test"},
    ],
)
async def test_poll_validation_422(
    api: httpx.AsyncClient, world: World, payload: dict[str, Any]
) -> None:
    assert (await api.post(POLL, json=payload)).status_code == 422


async def test_poll_takes_no_query_handle(api: httpx.AsyncClient, world: World) -> None:
    handle = (await _signup(api))["status_handle"]
    # the handle travels in the body only; a URL carrying it is refused before any lookup
    response = await api.post(POLL, params={"status_handle": handle}, json={})
    assert response.status_code == 422
    assert (await api.get(POLL, params={"status_handle": handle})).status_code == 405


async def test_poll_rate_limited_per_handle(api: httpx.AsyncClient, world: World) -> None:
    # 10 per clock minute per handle: 21 tries over at most two minutes put 11 in one of them,
    # so one is refused even when the minute rolls over mid-loop; the first ten never are.
    handle = (await _signup(api))["status_handle"]
    responses = [await _poll(api, handle) for _ in range(21)]
    statuses = [r.status_code for r in responses]
    assert statuses[:10] == [200] * 10, statuses
    assert set(statuses) <= {200, 429} and 429 in statuses, statuses
    limited = next(r for r in responses if r.status_code == 429)
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"]["type"] == "RATE_LIMITED"


async def test_poll_limit_counts_unknown_handles_too(api: httpx.AsyncClient, world: World) -> None:
    statuses = [(await _poll(api, "B" * 43)).status_code for _ in range(21)]
    assert statuses[:10] == [404] * 10, statuses
    assert 429 in statuses, statuses


async def test_handle_never_reaches_a_log_line(
    api: httpx.AsyncClient, world: World, auth_db: Conn, log_lines: Any
) -> None:
    capture = log_lines()
    handle = (await _signup(api))["status_handle"]
    assert (await _poll(api, handle)).status_code == 200
    assert (await _poll(api, "C" * 43)).status_code == 404
    text = capture.text()
    assert "/signup/status/poll" in text  # the access line names the route
    for secret in (handle, "C" * 43, _digest(handle).hex()):
        assert secret not in text, secret


# ---- Google: the pending outcome and the Google sign-up ----


GMAIL = "asha.rao@gmail.com"
GOOGLE_FORM = {"brand_name": "Asha Botanicals", "owner_name": "Asha Rao"}


async def test_google_signup_and_pending_outcome_issue_handles(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    browser = GoogleBrowser(gapi, gw)
    done = (await browser.sign_in(**{"as": GMAIL})).json()
    assert done["outcome"] == "needs_details", done
    created = await gapi.post(
        SIGNUP_GOOGLE,
        json={
            "details_handle": done["details"]["details_handle"],
            "binding": browser.secret,
            **GOOGLE_FORM,
        },
    )
    assert created.status_code == 202, created.text
    first = created.json()["status_handle"]
    assert _well_formed(first)
    out = (await _poll(gapi, first)).json()
    assert out["status"] == "pending" and out["brand_name"] == "Asha Botanicals"
    # Continue with Google again: the pending outcome carries a new handle, which replaces it
    again = (await browser.sign_in()).json()
    assert again["outcome"] == "pending"
    second = again["signup"]["status_handle"]
    assert _well_formed(second) and second != first
    assert (await _poll(gapi, first)).status_code == 404
    assert (await _poll(gapi, second)).json()["status"] == "pending"
    [(_, _, stored, _)] = _stored(gw.conn, GMAIL)
    assert bytes(stored) == _digest(second)


async def test_google_pending_outcome_of_a_rejected_request_has_no_handle(
    gapi: httpx.AsyncClient, gw: GoogleWorld
) -> None:
    response = await gapi.post(SIGNUP, json={**FORM, "email": "meera@gmail.com"})
    assert response.status_code == 202
    gw.conn.execute(
        "UPDATE signup_requests SET status = 'rejected', reason = 'No.', decided_at = now()"
    )
    body = (await GoogleBrowser(gapi, gw).sign_in(**{"as": "meera@gmail.com"})).json()
    assert body["outcome"] == "pending"
    assert body["signup"]["status"] == "rejected" and body["signup"]["status_handle"] is None
