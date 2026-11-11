"""Client item 2 (ADR-0019 amendment): a platform admin asks the applicant one question instead
of deciding, and the applicant sends one reply with the status handle, through the real app as
app_rw under FORCE RLS.

The request stays `pending` throughout, so the login, the Google outcome, the staff CLI and the
backlog count read it exactly as before. The poll shows the question (and the applicant's own
reply) only while pending; the reply needs only the handle, is capped at 1000 characters, is
stored once, and never reaches a log line. Both steps are audited; the reply's text is not.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import jwt
import pytest
from tests.conftest import CI_JWT_SECRET

from .auth_support import PREFIX, Conn, World, audit_rows, bearer, build_world, tokens_for
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

SIGNUP = f"{PREFIX}/signup"
POLL = f"{PREFIX}/signup/status/poll"
REPLY = f"{PREFIX}/signup/status/reply"
QUEUE = f"{PREFIX}/admin/signup-requests"
ADMIN = "operator@acme.test"
PASSWORD = "Quiet-Harbour-Lamp-7305"
FORM: dict[str, Any] = {
    "brand_name": "Kalakar Weaves",
    "owner_name": "Meera Iyer",
    "email": "founder@kalakar.test",
    "password": PASSWORD,
}
QUESTION = "Which marketplaces do you sell on today, and under which store name?"
ANSWER = "Amazon India as Kalakar Weaves, and our own website."
NON_ADMINS = ["owner@acme.test", "admin@acme.test", "owner@bharat.test", "reviewer@bharat.test"]


@pytest.fixture
def world(auth_db: Conn) -> World:
    world = build_world(auth_db)
    auth_db.execute("UPDATE users SET is_platform_admin = true WHERE email = %s", [ADMIN])
    return world


def _as(world: World, email: str) -> dict[str, str]:
    slug = email.split("@", 1)[1].split(".", 1)[0]
    return bearer(tokens_for(world, email, slug))


async def _signup(api: httpx.AsyncClient, **changes: Any) -> dict[str, Any]:
    response = await api.post(SIGNUP, json={**FORM, **changes})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def _ask(
    api: httpx.AsyncClient, world: World, request_id: str, question: str = QUESTION,
    *, who: str = ADMIN,
) -> httpx.Response:  # fmt: skip
    return await api.post(
        f"{QUEUE}/{request_id}/ask", json={"question": question}, headers=_as(world, who)
    )


async def _reply(api: httpx.AsyncClient, handle: str, text: str = ANSWER) -> httpx.Response:
    return await api.post(REPLY, json={"status_handle": handle, "reply": text})


def _row(conn: Conn, request_id: str) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT status, info_question, info_asked_at IS NOT NULL, info_reply, "
        "info_replied_at IS NOT NULL FROM signup_requests WHERE id = %s",
        [request_id],
    ).fetchone()
    assert row is not None
    return tuple(row)


# ---- POST /admin/signup-requests/{request_id}/ask ----


@pytest.mark.endpoint("admin_signup_requests_ask", case="success")
async def test_ask_keeps_the_request_pending_and_audits(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api)
    response = await _ask(api, world, body["request_id"])
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "pending" and out["decided_at"] is None
    assert out["question"] == QUESTION and out["asked_at"] is not None and out["reply"] is None
    assert _row(auth_db, body["request_id"]) == ("pending", QUESTION, True, None, False)
    [audit] = audit_rows(auth_db, "signup.info_requested")
    assert audit[0] is None and audit[1] == "staff" and audit[3] == ADMIN
    assert audit[9] == QUESTION
    # the queue's pending tab still lists it, with the question
    listed = (await api.get(QUEUE, params={"status": "pending"}, headers=_as(world, ADMIN))).json()
    [row] = listed["data"]
    assert row["question"] == QUESTION
    # the same question again answers the stored row, with no second audit row
    again = await _ask(api, world, body["request_id"])
    assert again.status_code == 200 and again.json() == out
    assert len(audit_rows(auth_db, "signup.info_requested")) == 1


@pytest.mark.endpoint("admin_signup_requests_ask", case="conflict")
async def test_ask_a_second_question_or_a_decided_request_409(
    api: httpx.AsyncClient, world: World
) -> None:
    first = await _signup(api)
    assert (await _ask(api, world, first["request_id"])).status_code == 200
    second = await _ask(api, world, first["request_id"], "Another question?")
    assert second.status_code == 409
    assert second.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    decided = await _signup(api, email="other@kalakar.test")
    rejected = await api.post(
        f"{QUEUE}/{decided['request_id']}/reject", json={"reason": "No."}, headers=_as(world, ADMIN)
    )
    assert rejected.status_code == 200
    assert (await _ask(api, world, decided["request_id"])).status_code == 409


@pytest.mark.endpoint("admin_signup_requests_ask", case="validation")
@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "   "}, {"question": "x" * 1001},
     {"question": QUESTION, "status": "approved"}],
)  # fmt: skip
async def test_ask_validation_422(
    api: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    request_id = (await _signup(api))["request_id"]
    response = await api.post(f"{QUEUE}/{request_id}/ask", json=body, headers=_as(world, ADMIN))
    assert response.status_code == 422


async def test_ask_unknown_id_404(api: httpx.AsyncClient, world: World) -> None:
    assert (await _ask(api, world, str(uuid.uuid4()))).status_code == 404


@pytest.mark.endpoint("admin_signup_requests_ask", case="wrong_role")
@pytest.mark.parametrize("who", NON_ADMINS)
async def test_non_admin_cannot_ask_403(
    api: httpx.AsyncClient, world: World, auth_db: Conn, who: str
) -> None:
    request_id = (await _signup(api))["request_id"]
    response = await _ask(api, world, request_id, who=who)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "PERMISSION_DENIED"
    assert _row(auth_db, request_id) == ("pending", None, False, None, False)


@pytest.mark.endpoint("admin_signup_requests_ask", case="wrong_tenant")
async def test_ask_from_any_tenant_touches_no_tenant(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    """The admin acts from acme; asking creates no tenant and no membership anywhere."""
    request_id = (await _signup(api))["request_id"]
    tenants = auth_db.execute("SELECT count(*) FROM tenants").fetchone()
    assert (await _ask(api, world, request_id)).status_code == 200
    assert auth_db.execute("SELECT count(*) FROM tenants").fetchone() == tenants
    members = (await api.get(f"{PREFIX}/members", headers=_as(world, "owner@acme.test"))).json()
    assert FORM["email"] not in {m["email"] for m in members["data"]}


@pytest.mark.endpoint("admin_signup_requests_ask", case="missing_auth")
async def test_ask_missing_auth_401(api: httpx.AsyncClient, world: World) -> None:
    response = await api.post(f"{QUEUE}/{uuid.uuid4()}/ask", json={"question": QUESTION})
    assert response.status_code == 401


def _claims(world: World, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": str(world.user(ADMIN)), "tid": str(world.tenant("acme")), "roles": ["operator"],
        "email": ADMIN, "typ": "access", "jti": str(uuid.uuid4()),
        "iss": "http://localhost:8000", "aud": "ai-marketplace-os", "iat": now, "exp": now + 600,
        **overrides,
    }  # fmt: skip
    return jwt.encode(claims, CI_JWT_SECRET, algorithm="HS256")


async def _ask_with(api: httpx.AsyncClient, token: str) -> int:
    response = await api.post(
        f"{QUEUE}/{uuid.uuid4()}/ask", json={"question": QUESTION}, headers=bearer(token)
    )
    return response.status_code


@pytest.mark.endpoint("admin_signup_requests_ask", case="invalid_token", variant="expired")
async def test_ask_expired_token_401(api: httpx.AsyncClient, world: World) -> None:
    now = int(time.time())
    assert await _ask_with(api, _claims(world, iat=now - 3600, exp=now - 1)) == 401


@pytest.mark.endpoint("admin_signup_requests_ask", case="invalid_token", variant="bad_signature")
async def test_ask_bad_signature_401(api: httpx.AsyncClient, world: World) -> None:
    forged = jwt.encode(
        jwt.decode(_claims(world), options={"verify_signature": False}),
        "another_secret_that_is_at_least_32_chars",
        algorithm="HS256",
    )
    assert await _ask_with(api, forged) == 401


@pytest.mark.endpoint("admin_signup_requests_ask", case="invalid_token", variant="wrong_typ")
async def test_ask_refresh_token_401(api: httpx.AsyncClient, world: World) -> None:
    assert await _ask_with(api, _claims(world, typ="refresh")) == 401


# ---- the waiting page: the poll shows the question; POST /signup/status/reply ----


@pytest.mark.endpoint("signup_status_reply", case="success")
async def test_applicant_reads_the_question_and_replies_once(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api)
    handle = body["status_handle"]
    before = (await api.post(POLL, json={"status_handle": handle})).json()
    assert before["question"] is None
    assert (await _ask(api, world, body["request_id"])).status_code == 200
    asked = (await api.post(POLL, json={"status_handle": handle})).json()
    assert asked["status"] == "pending" and asked["question"] == QUESTION
    assert asked["asked_at"] is not None and asked["reply"] is None
    response = await _reply(api, handle)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["status"] == "pending" and out["reply"] == ANSWER and out["replied_at"]
    assert _row(auth_db, body["request_id"]) == ("pending", QUESTION, True, ANSWER, True)
    # back with the admin, reply visible
    listed = (await api.get(QUEUE, params={"status": "pending"}, headers=_as(world, ADMIN))).json()
    [row] = listed["data"]
    assert row["reply"] == ANSWER and row["replied_at"] is not None
    [audit] = audit_rows(auth_db, "signup.info_replied")
    assert audit[0] is None and audit[1] == "system" and ANSWER not in str(audit)
    # the same reply again answers it again; nothing more is written
    again = await _reply(api, handle)
    assert again.status_code == 200 and again.json()["reply"] == ANSWER
    assert len(audit_rows(auth_db, "signup.info_replied")) == 1


async def test_reply_exposes_nothing_beyond_the_poll(api: httpx.AsyncClient, world: World) -> None:
    body = await _signup(api, phone="+91 98765 43210", website="https://kalakar.test")
    await _ask(api, world, body["request_id"])
    text = (await _reply(api, body["status_handle"])).text
    for leak in (FORM["email"], "Meera Iyer", "98765", "kalakar.test", body["request_id"]):
        assert leak not in text, leak


@pytest.mark.endpoint("signup_status_reply", case="conflict")
async def test_reply_without_a_question_twice_or_after_a_decision_409(
    api: httpx.AsyncClient, world: World, auth_db: Conn
) -> None:
    body = await _signup(api)
    handle = body["status_handle"]
    early = await _reply(api, handle)
    assert early.status_code == 409
    assert early.json()["error"]["type"] == "INVALID_STATUS_TRANSITION"
    await _ask(api, world, body["request_id"])
    assert (await _reply(api, handle)).status_code == 200
    assert (await _reply(api, handle, "Something else.")).status_code == 409
    assert _row(auth_db, body["request_id"])[3] == ANSWER
    other = await _signup(api, email="other@kalakar.test")
    await _ask(api, world, other["request_id"])
    await api.post(f"{QUEUE}/{other['request_id']}/approve", json={}, headers=_as(world, ADMIN))
    assert (await _reply(api, other["status_handle"])).status_code == 409
    decided = (await api.post(POLL, json={"status_handle": other["status_handle"]})).json()
    assert decided["status"] == "approved" and decided["question"] is None


async def test_reply_with_an_unknown_replaced_or_blank_handle_404(
    api: httpx.AsyncClient, world: World
) -> None:
    body = await _signup(api)
    await _ask(api, world, body["request_id"])
    blank = await _signup(api, password="Not-the-Password-0123")  # names nothing
    replaced = body["status_handle"]
    await _signup(api)  # a re-submit replaces the handle
    for handle in ("A" * 43, blank["status_handle"], replaced):
        response = await _reply(api, handle)
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "NOT_FOUND"


@pytest.mark.endpoint("signup_status_reply", case="validation")
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status_handle": "A" * 43},
        {"status_handle": "A" * 43, "reply": ""},
        {"status_handle": "A" * 43, "reply": "x" * 1001},
        {"status_handle": "short", "reply": "fine"},
        {"status_handle": "A" * 43, "reply": "fine", "email": "founder@kalakar.test"},
    ],
)
async def test_reply_validation_422(
    api: httpx.AsyncClient, world: World, payload: dict[str, Any]
) -> None:
    assert (await api.post(REPLY, json=payload)).status_code == 422


async def test_reply_takes_no_query_handle(api: httpx.AsyncClient, world: World) -> None:
    handle = (await _signup(api))["status_handle"]
    response = await api.post(REPLY, params={"status_handle": handle}, json={"reply": "x"})
    assert response.status_code == 422


async def test_reply_rate_limited_per_handle(api: httpx.AsyncClient, world: World) -> None:
    # 5 per clock minute per handle: 11 tries put 6 in one minute even across a rollover
    statuses = [(await _reply(api, "B" * 43)).status_code for _ in range(11)]
    assert statuses[:5] == [404] * 5, statuses
    assert 429 in statuses, statuses


async def test_reply_never_reaches_a_log_line(
    api: httpx.AsyncClient, world: World, log_lines: Any
) -> None:
    body = await _signup(api)
    await _ask(api, world, body["request_id"])
    capture = log_lines()
    assert (await _reply(api, body["status_handle"])).status_code == 200
    text = capture.text()
    assert "/signup/status/reply" in text
    for secret in (body["status_handle"], ANSWER):
        assert secret not in text, secret
