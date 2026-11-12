"""Chrome pass 2 (12 Nov): the Marketplaces page showed Region "—" for the connected Amazon India
account. Amazon's OAuth reply carries no region, so `marketplace_accounts.region` is always null;
the API now names the deployment's marketplace instead (`account_out`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.api.v1.routers.marketplaces import account_out
from app.core.config import Settings, get_settings
from app.core.flags import account_marketplace
from app.services.marketplaces import AccountView

LOCAL_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/marketplace_os"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    env = {"APP_ENV": "local", "APP_ROLE": "api", "DATABASE_URL": LOCAL_URL}
    env["JWT_SECRET"] = "Zq9-not-committed-private-secret-32+"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _view(marketplace: str, region: str | None = None) -> AccountView:
    return AccountView(
        id=uuid.uuid4(),
        marketplace=marketplace,
        seller_id="A3JXC977HPZQEN",
        region=region,
        status="active",
        needs_reconnect_since=None,
        expires_at=None,
        created_at=datetime(2026, 11, 9, 8, 44, tzinfo=UTC),
    )


def test_amazon_account_names_india_and_its_marketplace_id() -> None:
    out = account_out(_view("amazon"))
    assert out.region == "India"
    assert out.marketplace_id == "A21TJRUUN4KGV"


def test_a_stored_region_wins() -> None:
    assert account_out(_view("amazon", region="eu")).region == "eu"


def test_flipkart_is_india_with_no_marketplace_id() -> None:
    out = account_out(_view("flipkart"))
    assert (out.region, out.marketplace_id) == ("India", None)


@pytest.mark.parametrize(
    ("marketplace_id", "region"),
    [("A21TJRUUN4KGV", "India"), ("A1F83G8C2ARO7P", "United Kingdom"), ("ZZZ", None)],
)
def test_amazon_marketplace_ids_map_to_their_country(
    marketplace_id: str, region: str | None
) -> None:
    settings: Settings = get_settings().model_copy(update={"amazon_marketplace_id": marketplace_id})
    assert account_marketplace("amazon", settings) == (marketplace_id, region)
