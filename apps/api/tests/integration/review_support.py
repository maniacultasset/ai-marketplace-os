"""The S19 review test world: S18's generation world (the demo seed as `app_rw`, the stub pipeline,
no model key) plus helpers that make generated listings and call the approve service.

`validated_listing` creates a fresh product and runs one real `listing_generate` job through the
stub, so the version, its maker, its passed `rules_engine` run and the run's pins are exactly what
production writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .generation_support import GenWorld, make_generating

OPERATOR = "operator@acme.test"
OWNER = "owner@acme.test"
REVIEWER = "reviewer@acme.test"
ADMIN = "admin@acme.test"
CATEGORY = "ethnic_wear_kurta_sets"


@dataclass(frozen=True)
class Made:
    listing_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID


async def new_product(world: GenWorld, email: str = OPERATOR, sku: str | None = None) -> str:
    """A product of the caller's tenant (the products service, as `app_rw`); returns its SKU."""
    from app.services.products import NewProduct, create_product

    sku = sku or f"S19-{uuid.uuid4().hex[:10].upper()}"
    await create_product(
        world.principal(email),
        NewProduct(
            sku=sku,
            title="Cotton Kurta Set",
            category=CATEGORY,
            images=(),
            attributes={"color": "blue", "fabric_type": "cotton"},
        ),
    )
    return sku


async def run_job(job_id: uuid.UUID) -> None:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job as run

    register_bodies()
    await run(str(job_id))


async def generated(
    world: GenWorld,
    email: str = OPERATOR,
    *,
    scenario: str | None = None,
    sku: str | None = None,
) -> Made:
    """A generated listing (status per the stub scenario: `validated` for none)."""
    sku = sku or await new_product(world, email)
    accepted = await make_generating(world, email=email, sku=sku, instructions=scenario)
    await run_job(accepted.job_id)
    row = world.listing(accepted.listing_id)
    assert row["current_version_id"] is not None, row["status"]
    return Made(accepted.listing_id, row["current_version_id"], accepted.job_id)


def item(made: Made, decision: str = "approve", **extra: Any) -> dict[str, Any]:
    return {
        "listing_id": str(made.listing_id),
        "listing_version_id": str(made.version_id),
        "decision": decision,
        **extra,
    }


def rows(world: GenWorld, query: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    return world.conn.execute(query, params or []).fetchall()
