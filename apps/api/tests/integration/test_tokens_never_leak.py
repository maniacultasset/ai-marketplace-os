"""S15 T17 (DW4, AC6; invariant 6): marketplace token values, OAuth codes and states never reach a
log line, a Sentry envelope, a response body or header, an audit row, a human task, an error code
column or the stored ciphertext. The fake marketplace marks every token `CANARYzq9-<hex>`."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.conftest import LogCapture, SentryCapture

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .marketplace_support import (
    MktWorld,
    as_member,
    callback,
    confirm,
    connect,
    consent,
    mkt,
    pending_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mkt"]
CANARY = "CANARYzq9"
DSN = "https://public@o0.ingest.example.invalid/0"


@pytest.fixture
async def leak_api(
    mkt: MktWorld, sentry_events: SentryCapture, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The app with Sentry on (in-memory transport, no network)."""
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("SENTRY_DSN", DSN)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.7", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


class Recorder:
    """Every response body and header the flow saw (the connect body excepted from the state
    check: it is the one channel that carries the state, to the consent page)."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.connect_bodies: list[str] = []

    def add(self, response: httpx.Response, *, connect_body: bool = False) -> httpx.Response:
        (self.connect_bodies if connect_body else self.texts).append(response.text)
        self.texts.append(json.dumps(dict(response.headers)))
        return response


def _db_texts(mw: MktWorld) -> list[str]:
    out: list[str] = []
    for row in mw.conn.execute(
        "SELECT coalesce(before::text, ''), coalesce(after::text, ''), coalesce(reason, ''), "
        "action FROM audit_log"
    ).fetchall():
        out.extend(str(v) for v in row)
    for row in mw.conn.execute(
        "SELECT question, reason, expected_answer::text, coalesce(answer::text, ''), "
        "suggestions::text, evidence::text FROM human_tasks"
    ).fetchall():
        out.extend(str(v) for v in row)
    for row in mw.conn.execute(
        "SELECT coalesce(last_refresh_error_code, ''), "
        "convert_from(coalesce(token_enc, ''::bytea), 'SQL_ASCII') FROM marketplace_accounts"
    ).fetchall():
        out.extend(str(v) for v in row)
    for row in mw.conn.execute(
        "SELECT convert_from(coalesce(token_enc, ''::bytea), 'SQL_ASCII') FROM oauth_states"
    ).fetchall():
        out.extend(str(v) for v in row)
    return out


def _due(mw: MktWorld) -> None:
    mw.conn.execute("UPDATE marketplace_accounts SET expires_at = now() + interval '1 minute'")


async def test_tokens_never_leak(
    mkt: MktWorld,
    leak_api: httpx.AsyncClient,
    sentry_events: SentryCapture,
    log_lines: Callable[..., LogCapture],
) -> None:
    from app.workers.token_refresh import refresh_account

    capture = log_lines()
    rec = Recorder()
    owner = as_member(mkt, "owner@acme.test")
    secrets_seen: list[str] = []

    async def one_connect(seller: str) -> str:
        response = rec.add(
            await leak_api.post(
                f"{PREFIX}/marketplaces/connect", json={"marketplace": "amazon"}, headers=owner
            ),
            connect_body=True,
        )
        query = await consent(mkt, response.json()["authorize_url"], seller)
        secrets_seen.extend([query["state"], query["spapi_oauth_code"]])
        handle = pending_of(rec.add(await callback(leak_api, "amazon", query)))
        # a replayed callback and a malformed one
        rec.add(await callback(leak_api, "amazon", query))
        rec.add(await callback(leak_api, "amazon", {**query, "selling_partner_id": "A/../B"}))
        return handle

    handle = await one_connect("A1LEAK")
    confirmed = rec.add(await confirm(leak_api, mkt, "owner@acme.test", handle))
    account_id = uuid.UUID(confirmed.json()["id"])
    tenant = mkt.tenant("acme")
    for outcome in ("ok", "server_error", "invalid_grant"):
        _due(mkt)
        mkt.control.next_refresh = outcome
        await refresh_account(tenant, account_id)
    reconnect = await one_connect("A1LEAK")
    rec.add(await confirm(leak_api, mkt, "owner@acme.test", reconnect))
    rec.add(await leak_api.get(f"{PREFIX}/marketplaces/accounts", headers=owner))
    rec.add(
        await leak_api.post(f"{PREFIX}/marketplaces/accounts/{account_id}/revoke", headers=owner)
    )
    import sentry_sdk

    sentry_sdk.get_client().flush(timeout=2)

    issued = [r for r in mkt.control.token_requests if "refresh_token" in r]
    assert issued, "the flow made no refresh call"
    corpus = {
        "logs": capture.text(),
        "sentry": sentry_events.serialized(),
        "responses": "\n".join(rec.texts),
        "db": "\n".join(_db_texts(mkt)),
    }
    for name, text in corpus.items():
        assert CANARY not in text, name
        for value in secrets_seen:
            assert value not in text, (name, value[:6])
    assert CANARY not in "\n".join(rec.connect_bodies)
    assert sentry_events.events(), "the invalid_grant refresh raised no alert event"


async def test_callback_request_line_redacted(
    mkt: MktWorld,
    leak_api: httpx.AsyncClient,
    log_lines: Callable[..., LogCapture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redaction still covers httpx request lines if the library is turned up: S20 keeps
    httpx at WARNING by default (`test_http_libraries_quiet_by_default`), so this test raises it
    to INFO to keep the redaction proof non-vacuous."""
    capture = log_lines()  # configures logging, httpx at WARNING
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.INFO)
    query = await consent(mkt, await connect(leak_api, mkt))
    flipkart = await consent(mkt, await connect(leak_api, mkt, marketplace="flipkart"))
    pending_of(await callback(leak_api, "amazon", query))
    pending_of(await callback(leak_api, "flipkart", flipkart))
    text = capture.text()
    for value in (query["state"], query["spapi_oauth_code"], flipkart["state"], flipkart["code"]):
        assert value not in text
    access = [
        line
        for line in capture.lines()
        if line.get("route") == "/marketplaces/{marketplace}/callback"
    ]
    assert access, [line.get("route") for line in capture.lines()]
    assert all("?" not in json.dumps(line) for line in access)
    # Since S16 the HTTP client loggers sit at WARNING (app/core/logging.py), so the client's own
    # request line is no longer written at all; any client line that does appear is redacted.
    client_lines = [line["message"] for line in capture.lines() if "/callback?" in line["message"]]
    for line in client_lines:
        assert "state=[REDACTED]" in line
        assert "spapi_oauth_code=[REDACTED]" in line or "code=[REDACTED]" in line


async def test_location_pending_not_logged(
    mkt: MktWorld,
    leak_api: httpx.AsyncClient,
    sentry_events: SentryCapture,
    log_lines: Callable[..., LogCapture],
) -> None:
    capture = log_lines()
    query = await consent(mkt, await connect(leak_api, mkt))
    handle = pending_of(await callback(leak_api, "amazon", query))
    rec = await confirm(leak_api, mkt, "admin@acme.test", handle)  # 409: another user
    assert rec.status_code == 409
    assert handle not in capture.text()
    assert handle not in sentry_events.serialized()


def _context_fixture(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "packages/schemas/fixtures/GenerationContext.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _field_names(model: Any, seen: set[Any]) -> set[str]:
    from pydantic import BaseModel

    names: set[str] = set()
    if model in seen:
        return names
    seen.add(model)
    for name, field in model.model_fields.items():
        names.add(name)
        for arg in _types(field.annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _field_names(arg, seen)
    return names


def _types(annotation: Any) -> list[Any]:
    from typing import get_args

    found = [annotation]
    for arg in get_args(annotation):
        found.extend(_types(arg))
    return found


def test_generation_context_cannot_hold_tokens(repo_root: Path) -> None:
    from mkos_schemas.jobs import GenerationContext
    from pydantic import ValidationError

    names = _field_names(GenerationContext, set())
    banned = ("token", "secret", "credential", "account", "seller_id", "kms", "verifier")
    assert not [n for n in names if any(word in n for word in banned)]
    data = _context_fixture(repo_root)
    GenerationContext.model_validate(data)
    for key, value in (
        ("product", {"access_token": f"{CANARY}-1"}),
        ("brand_profile", {"refresh_token": f"{CANARY}-2"}),
        ("product", {"notes": json.dumps({"lwa_client_secret": "x"})}),
    ):
        with pytest.raises(ValidationError) as info:
            GenerationContext.model_validate({**data, key: value})
        assert CANARY not in str(info.value)


def test_marketplace_account_not_in_seam_types(repo_root: Path) -> None:
    """The AI seam package never models an account or a token (CANON.md:125)."""
    source = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo_root / "packages/schemas/src/mkos_schemas").rglob("*.py")
    )
    for name in ("MarketplaceAccount", "token_enc", "kms_key_version", "TokenPair"):
        assert name not in source


def test_http_libraries_quiet_by_default(leak_api: httpx.AsyncClient) -> None:
    """S20: the marketplace client writes its own `marketplace.call` line, so httpx and httpcore
    never log a request line (seller id and SKU in the URL) below WARNING."""
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
