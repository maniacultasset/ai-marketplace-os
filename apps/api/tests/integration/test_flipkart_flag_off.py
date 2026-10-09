"""S22 T3 (AC2; D2, D5; C4, C5): with `FLIPKART_ENABLED=false`, Flipkart connect, callback and
confirm answer the one 501 envelope, write nothing, consume nothing and send nothing to the
marketplace. Amazon is untouched. The in-process fake counts every token request."""

from __future__ import annotations

import httpx
import pytest

from .auth_support import PREFIX
from .conftest import LOCAL_HOSTS
from .marketplace_support import (
    MktWorld,
    callback,
    confirm,
    connect,
    consent,
    mapi,
    mkt,
    oauth_rows,
    pending_of,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["mapi", "mkt"]
OWNER = "owner@acme.test"


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("FLIPKART_ENABLED", "true" if on else "false")
    get_settings.cache_clear()


def _not_enabled(response: httpx.Response) -> None:
    assert response.status_code == 501, response.text
    error = response.json()["error"]
    assert error["type"] == "NOT_IMPLEMENTED", error
    assert error["message"] == "Flipkart is not enabled", error
    assert error["error_report"]["error_code"] == "NOT_IMPLEMENTED", error


async def test_flipkart_flag_off_connect(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .marketplace_support import as_member

    _flag(monkeypatch, False)
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect",
        json={"marketplace": "flipkart"},
        headers=as_member(mkt, OWNER),
    )
    _not_enabled(response)
    assert oauth_rows(mkt.conn) == []
    assert mkt.control.token_requests == [] and mkt.control.exchanges == 0


async def test_flipkart_flag_off_callback(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await connect(mapi, mkt, OWNER, "flipkart")
    query = await consent(mkt, url)
    _flag(monkeypatch, False)
    _not_enabled(await callback(mapi, "flipkart", query))
    rows = oauth_rows(mkt.conn)
    assert len(rows) == 1 and rows[0]["token_enc"] is None, "the state row was consumed"
    assert mkt.control.exchanges == 0 and mkt.control.token_requests == []
    # the flag back on: the same state still completes
    _flag(monkeypatch, True)
    assert (await callback(mapi, "flipkart", query)).status_code == 303


async def test_flipkart_flag_off_confirm(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await connect(mapi, mkt, OWNER, "flipkart")
    handle = pending_of(await callback(mapi, "flipkart", await consent(mkt, url)))
    _flag(monkeypatch, False)
    _not_enabled(await confirm(mapi, mkt, OWNER, handle))
    rows = oauth_rows(mkt.conn)
    assert len(rows) == 1 and rows[0]["token_enc"] is not None, "the pending row was consumed"
    assert mkt.conn.execute("SELECT count(*) FROM marketplace_accounts").fetchone() == (0,)
    _flag(monkeypatch, True)
    response = await confirm(mapi, mkt, OWNER, handle)
    assert response.status_code == 200, response.text
    assert response.json()["marketplace"] == "flipkart"


async def test_flipkart_flag_off_callback_422_first(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag(monkeypatch, False)
    response = await callback(mapi, "flipkart", {"state": "s"})
    assert response.status_code == 422, response.text
    response = await callback(
        mapi, "flipkart", {"state": "s", "code": "a", "spapi_oauth_code": "b"}
    )
    assert response.status_code == 422, response.text


async def test_flipkart_flag_off_connect_401_and_403_first(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .marketplace_support import as_member

    _flag(monkeypatch, False)
    body = {"marketplace": "flipkart"}
    response = await mapi.post(f"{PREFIX}/marketplaces/connect", json=body)
    assert response.status_code == 401, response.text
    response = await mapi.post(
        f"{PREFIX}/marketplaces/connect", json=body, headers=as_member(mkt, "operator@acme.test")
    )
    assert response.status_code == 403, response.text
    response = await mapi.post(f"{PREFIX}/marketplaces/connect/confirm", json={"pending": "x" * 43})
    assert response.status_code == 401, response.text


async def test_amazon_unaffected_by_flipkart_flag(
    mkt: MktWorld, mapi: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _flag(monkeypatch, False)
    url = await connect(mapi, mkt, OWNER, "amazon")
    handle = pending_of(await callback(mapi, "amazon", await consent(mkt, url)))
    response = await confirm(mapi, mkt, OWNER, handle)
    assert response.status_code == 200, response.text


def test_flipkart_ops_declare_501() -> None:
    from app.api.v1.surface import OPS
    from app.main import create_app

    spec = create_app().openapi()
    by_id = {op["operationId"]: op for path in spec["paths"].values() for op in path.values()}
    for op_id in ("marketplaces_connect", "marketplaces_callback", "marketplaces_connect_confirm"):
        assert OPS[op_id].error_codes.get(501) == ("NOT_IMPLEMENTED",), op_id
        assert "501" in by_id[op_id]["responses"], op_id
