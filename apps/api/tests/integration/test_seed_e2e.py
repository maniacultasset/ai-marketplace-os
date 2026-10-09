"""S9 T24 (AC 5, 7, 13): the local smoke. After `seed --profile demo`, the application path
(unit_of_work + repositories) shows each tenant its own catalogue, its own brand chunks and
the effective rules, and never the other tenant's rows."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS

pytestmark = [
    pytest.mark.allow_hosts(LOCAL_HOSTS),
    pytest.mark.usefixtures("seed_env", "clean_tenant_db"),
]


@pytest.fixture
def seed_env(db_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.core.config import get_settings

    monkeypatch.setenv("ALEMBIC_DATABASE_URL", os.environ["DATABASE_URL"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_seed_demo_end_to_end() -> None:
    from app.cli import run_seed
    from app.core.vectors import hash_vector
    from app.db.models.catalog import Product
    from app.db.seed_ops import seed_tenant_id
    from app.db.session import unit_of_work
    from app.repositories.base import TenantScopedRepository
    from app.repositories.brand_chunks import BrandChunkRepository
    from app.repositories.rules import RuleRepository

    class ProductRepository(TenantScopedRepository[Product]):
        model = Product

    assert await run_seed("demo", reset=False) == 0
    acme, bharat = seed_tenant_id("acme"), seed_tenant_id("bharat")

    async with unit_of_work(bharat) as session:
        bharat_ids = {p.id for p in await ProductRepository(session, bharat).list()}
    async with unit_of_work(acme) as session:
        products = await ProductRepository(session, acme).list()
        assert sorted(p.sku for p in products) == [
            "ACME-900",
            "DECOR-001",
            "KURTA-001",
            "KURTA-002",
            # S20 T21: the demo kurta family (style and its 4 children)
            "KURTA-FAM-01",
            "KURTA-FAM-01-M-MAROON",
            "KURTA-FAM-01-M-NAVY-BLUE",
            "KURTA-FAM-01-S-MAROON",
            "KURTA-FAM-01-S-NAVY-BLUE",
        ]
        assert not {p.id for p in products} & bharat_ids
        for other in bharat_ids:
            assert await ProductRepository(session, acme).get_by_id(other) is None

        chunks = await BrandChunkRepository(session, acme).search(hash_vector("acme:tone_rule:0"))
        assert chunks[0].type == "tone_rule"
        assert {c.tenant_id for c in chunks} == {acme}

        rules = await RuleRepository(session, acme).list_effective(
            "amazon", "ethnic_wear_kurta_sets"
        )
        banned = [r for r in rules if r.rule_type == "banned_terms"]
        assert banned and all(r.tenant_id is None for r in banned)
        # S14a T12 adds a `*` banned row, which sorts before `content.title`.
        assert {r.field for r in banned} == {"*", "content.title"}
        (title_banned,) = [r for r in banned if r.field == "content.title"]
        assert "guaranteed" in title_banned.rule_def["terms"]
