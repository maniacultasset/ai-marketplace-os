#!/usr/bin/env python
"""make seed  ->  uv run python scripts/seed.py --profile isolation --reset

Two tenants holding the SAME SKUs, so the Day-5 isolation gate has a fixture
that fails loudly when a tenant filter goes missing. Deterministic UUIDs, so
re-running updates rather than duplicating. No GCP and no LLM key required.
`python -m app.cli seed` is a thin wrapper over seed().
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from pwdlib import PasswordHash
from sqlalchemy import delete, func, select

from app.db.session import dispose_engine, platform_scope
from app.models import Product, Rule, Tenant, User

NS = uuid.UUID("7b3c1f2e-0a44-4a1b-9c7d-2f5e8a0b1c33")
PASSWORD = "devpassword123"      # local only; staging seeds read SEED_PASSWORD
TENANTS = (("acme", "Acme Apparel"), ("bharat", "Bharat Home"))
ROLES = ("owner", "operator", "reviewer", "admin")
SHARED = (("KURTA-001", "Cotton Kurta Set", "ethnic_wear_kurta_sets"),
          ("KURTA-002", "Silk Kurta Set", "ethnic_wear_kurta_sets"),
          ("DECOR-001", "Brass Table Lamp", "home_decor_lamps"))
EXTRA = {"acme": ("ACME-900", "Linen Nehru Jacket", "ethnic_wear_kurta_sets"),
         "bharat": ("BH-900", "Jute Floor Rug", "home_decor_rugs")}
RULES_JSON = Path(__file__).resolve().parents[1] / "packages/schemas/fixtures/rules.seed.json"


def det(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NS, "|".join(parts))


async def seed(profile: str, reset: bool) -> None:
    hashed = PasswordHash.recommended().hash(PASSWORD)
    async with platform_scope(readonly=False) as s:
        if reset:
            for model in (Product, User, Rule, Tenant):
                await s.execute(delete(model))
        for row in json.loads(RULES_JSON.read_text()):
            await s.merge(Rule(**{**row, "id": uuid.UUID(row["id"])}))
        for slug, name in TENANTS:
            tid = det("tenant", slug)
            await s.merge(Tenant(id=tid, name=name, tone="warm, plain Indian English",
                                 guidelines=[], glossary={}))
            for role in ROLES:
                await s.merge(User(id=det("user", slug, role), tenant_id=tid, role=role,
                                   email=f"{role}@{slug}.test", hashed_password=hashed))
            skus = list(SHARED) + ([EXTRA[slug]] if profile == "demo" else [])
            for sku, title, category in skus:
                # The same SKU strings under BOTH tenants: that collision IS the fixture.
                await s.merge(Product(id=det("product", slug, sku), tenant_id=tid, sku=sku,
                                      title=f"{name} {title}", status="draft", images=[],
                                      attributes={"category": category, "fabric": "cotton"}))

    async with platform_scope() as s:
        dupes = (await s.execute(
            select(Product.sku).group_by(Product.sku).having(func.count() > 1))).scalars().all()
        total = (await s.execute(select(func.count()).select_from(Product))).scalar_one()
        assert len(dupes) == len(SHARED), f"expected {len(SHARED)} cross-tenant SKUs, got {dupes}"
        print(f"seeded profile={profile} tenants={len(TENANTS)} "
              f"users={len(TENANTS) * len(ROLES)} products={total} "
              f"shared_skus={sorted(dupes)} login=owner@acme.test/{PASSWORD}")
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("demo", "isolation"), default="isolation")
    parser.add_argument("--reset", action="store_true", help="delete app rows first")
    args = parser.parse_args()
    asyncio.run(seed(args.profile, args.reset))
