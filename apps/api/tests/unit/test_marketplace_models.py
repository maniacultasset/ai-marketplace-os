"""S15 T6 (AC11; D2, D4): the confirm body, the callback's Amazon parameters and the revoke's
OpenAPI description, checked on the app (no database: the presence-only fake auth)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

PREFIX = "/api/v1"
BEARER = {"Authorization": "Bearer test"}
GOOD = "A" * 20 + "b" * 20 + "_-9"  # 43 characters, the token_urlsafe(32) alphabet


@pytest.fixture(scope="module")
def client(v1_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(v1_app, raise_server_exceptions=False) as c:
        yield c


def test_confirm_request_accepts_a_handle() -> None:
    from app.api.v1.schemas.marketplaces import ConnectConfirmRequest

    assert len(GOOD) == 43
    assert ConnectConfirmRequest(pending=GOOD).pending == GOOD


@pytest.mark.parametrize(
    "pending",
    ["A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + "+", "A" * 42 + "/", "A" * 42 + "é", " " * 43],
    ids=["42", "44", "padding", "plus", "slash", "unicode", "blank"],
)
def test_confirm_request_pattern(pending: str) -> None:
    from app.api.v1.schemas.marketplaces import ConnectConfirmRequest

    with pytest.raises(ValidationError):
        ConnectConfirmRequest(pending=pending)


def test_confirm_request_forbids_extra() -> None:
    from app.api.v1.schemas.marketplaces import ConnectConfirmRequest

    with pytest.raises(ValidationError):
        ConnectConfirmRequest.model_validate({"pending": GOOD, "tenant_id": "x"})


def _callback(client: TestClient, **params: str) -> Any:
    return client.get(f"{PREFIX}/marketplaces/amazon/callback", params=params)


@pytest.mark.parametrize(
    "params",
    [{"state": "s"}, {"state": "s", "code": "c", "spapi_oauth_code": "c"}],
    ids=["none", "both"],
)
def test_callback_code_exactly_one(client: TestClient, params: dict[str, str]) -> None:
    response = _callback(client, **params)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "VALIDATION_FAILED"


def test_callback_amazon_params_accepted(client: TestClient) -> None:
    # valid shape: no client is configured in the unit app, so the route answers 501 (no DB)
    response = _callback(client, state="s", spapi_oauth_code="c", selling_partner_id="A1SELLER")
    assert response.status_code == 501, response.text


@pytest.mark.parametrize(
    "seller",
    ["A1/B", "../x", "A1 B", "A" * 65, "Ä1", ""],
    ids=["slash", "dotdot", "space", "65", "unicode", "empty"],
)
def test_callback_seller_param_pattern(client: TestClient, seller: str) -> None:
    response = _callback(client, state="s", spapi_oauth_code="c", selling_partner_id=seller)
    assert response.status_code == 422, response.text
    assert seller not in response.text or not seller


def test_callback_oversized_spapi_code(client: TestClient) -> None:
    response = _callback(client, state="s", spapi_oauth_code="c" * 2049)
    assert response.status_code == 422


def test_revoke_op_description_names_marketplace_removal(v1_app: FastAPI) -> None:
    spec = v1_app.openapi()
    revoke = spec["paths"][f"{PREFIX}/marketplaces/accounts/{{account_id}}/revoke"]["post"]
    text = revoke["description"]
    assert "stored tokens" in text
    assert "Seller Central" in text and "Flipkart" in text


def test_confirm_declares_both_409_codes(v1_app: FastAPI) -> None:
    spec = v1_app.openapi()
    confirm = spec["paths"][f"{PREFIX}/marketplaces/connect/confirm"]["post"]
    description = confirm["responses"]["409"]["description"]
    assert "MARKETPLACE_ACCOUNT_CONFLICT" in description
    assert "OAUTH_STATE_INVALID" in description


def test_account_out_has_no_secret_fields_s15() -> None:
    from app.api.v1.schemas.marketplaces import MarketplaceAccountOut

    banned = ("token", "secret", "key", "enc", "verifier", "hash")
    for name in MarketplaceAccountOut.model_fields:
        assert not any(word in name for word in banned), name
