"""S3 T4 (AC7; D7): keyset pages `{data, page: {next_cursor, has_more, limit}}`, limit 1..200."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated, Any

import pytest
from app.api.errors import ApiError, install_error_handlers
from app.api.pagination import (
    DEFAULT_LIMIT,
    MAX_CURSOR_LENGTH,
    MAX_LIMIT,
    Page,
    PageInfo,
    PageParams,
    decode_cursor,
    encode_cursor,
)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.base import WireModel
from mkos_schemas.codes import ErrorCode


class Item(WireModel):
    id: uuid.UUID


class ItemPage(Page[Item]):
    pass


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/items", response_model=ItemPage)
    async def items(page: Annotated[PageParams, Depends()]) -> ItemPage:
        return ItemPage(data=[], page=PageInfo(next_cursor=None, has_more=False, limit=page.limit))

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_default_and_bounds(client: TestClient) -> None:
    assert DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 200
    assert client.get("/items").json()["page"]["limit"] == 50
    for good in (1, 200):
        assert client.get(f"/items?limit={good}").json()["page"]["limit"] == good


@pytest.mark.parametrize("bad", ["0", "201", "-1", "abc", "1e3", ""])
def test_bad_limit_422(client: TestClient, bad: str) -> None:
    response = client.get(f"/items?limit={bad}")
    assert response.status_code == 422
    assert response.json()["error"]["type"] == ErrorCode.VALIDATION_FAILED.value


def _bad_cursors() -> list[Any]:
    # The random-id cursors carry fixed ids: every pytest-xdist worker must collect the same names.
    naive = pytest.param(
        _b64(json.dumps({"t": "2026-10-05T10:00:00", "id": str(uuid.uuid4())}).encode()),
        id="naive-timestamp",
    )
    return [
        "",
        "not base64!!",
        "%%%",
        _b64(b"junk"),
        _b64(b"[1, 2]"),
        _b64(json.dumps({"t": "x", "id": "y"}).encode()),
        naive,
        pytest.param(_b64(json.dumps({"t": "NaN", "id": str(uuid.uuid4())}).encode()), id="nan-t"),
        pytest.param(
            _b64(
                json.dumps({"t": "2026-10-05T10:00:00Z", "id": str(uuid.uuid4()), "x": 1}).encode()
            ),
            id="extra-key",
        ),
        "a" * (MAX_CURSOR_LENGTH + 1),
        "é" * 10,
        _b64(b"\xff\xfe"),
    ]


@pytest.mark.parametrize("cursor", _bad_cursors())
def test_bad_cursor_422(client: TestClient, cursor: str) -> None:
    response = client.get("/items", params={"cursor": cursor})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["type"] == ErrorCode.VALIDATION_FAILED.value
    assert body["details"][0]["loc"] == ["query", "cursor"]


def test_valid_cursor_accepted(client: TestClient) -> None:
    cursor = encode_cursor(datetime(2026, 10, 5, tzinfo=UTC), uuid.uuid4())
    assert client.get("/items", params={"cursor": cursor}).status_code == 200


def test_cursor_has_no_padding_and_is_urlsafe() -> None:
    cursor = encode_cursor(datetime(2026, 10, 5, 1, 2, 3, 456, tzinfo=UTC), uuid.uuid4())
    assert "=" not in cursor
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_page_component_name() -> None:
    assert ItemPage.__name__ == "ItemPage"
    schema = _app().openapi()["components"]["schemas"]
    assert "ItemPage" in schema
    assert set(schema["ItemPage"]["properties"]) == {"data", "page"}
    assert set(schema["PageInfo"]["properties"]) == {"next_cursor", "has_more", "limit"}


@settings(max_examples=200, deadline=None)
@given(
    st.datetimes(
        min_value=datetime(1970, 1, 2),
        max_value=datetime(9999, 12, 30),
        timezones=st.sampled_from([UTC, timezone(timedelta(hours=5, minutes=30))]),
    ),
    st.uuids(),
)
def test_cursor_roundtrip(created_at: datetime, row_id: uuid.UUID) -> None:
    got_at, got_id = decode_cursor(encode_cursor(created_at, row_id))
    assert got_at == created_at
    assert got_id == row_id


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=600))
def test_decode_never_raises_unexpected(raw: str) -> None:
    try:
        created_at, row_id = decode_cursor(raw)
    except ApiError as exc:
        assert exc.status == 422
        assert exc.code is ErrorCode.VALIDATION_FAILED
    else:
        assert created_at.tzinfo is not None
        assert isinstance(row_id, uuid.UUID)
