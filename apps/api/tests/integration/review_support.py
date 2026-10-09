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


async def post_approve(
    api: Any,
    world: GenWorld,
    email: str,
    items: list[dict[str, Any]],
    *,
    key: str | None = None,
) -> Any:
    """`POST /api/v1/listings/approve` as `email` with a fresh (or the given) key."""
    from .products_support import bearer_for

    headers = {**bearer_for(world, email), "Idempotency-Key": key or str(uuid.uuid4())}
    return await api.post("/api/v1/listings/approve", headers=headers, json={"items": items})


def only_item(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    return items[0]  # type: ignore[no-any-return]


def audits(world: GenWorld, listing_id: uuid.UUID, action: str) -> int:
    return world.count("audit_log", "entity_id = %s AND action = %s", [listing_id, action])


def decisions(world: GenWorld, listing_id: uuid.UUID, decision: str = "approve") -> int:
    return world.count(
        "review_decisions", "listing_id = %s AND decision = %s", [listing_id, decision]
    )


async def edited(api: Any, world: GenWorld, made: Made, email: str = OWNER) -> Made:
    """A PATCH of the content by `email` (S18's route): the new version's editor is `email`."""
    from .products_support import bearer_for

    headers = {**bearer_for(world, email), "If-Match": f'"{made.version_id}"'}
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={
            "content": {
                "title": f"Cotton Kurta Set {uuid.uuid4().hex[:6]}",
                "bullets": ["Soft cotton fabric"],
                "description": "A cotton kurta set with a straight fit.",
            }
        },
    )
    assert response.status_code == 200, response.text
    return Made(made.listing_id, uuid.UUID(response.json()["current_version_id"]), made.job_id)


async def open_issue(world: GenWorld, made: Made, **over: Any) -> uuid.UUID:
    """An open issue on the listing (the repository, as `app_rw`); a warning by default."""
    from app.repositories.listing_issues import NewIssue
    from app.repositories.uow import tenant_uow

    field = over.pop("field_path", "content.title")
    code = over.pop("error_code", "VALUE_NOT_ALLOWED")
    source = over.pop("source", "rules_engine")
    values: dict[str, Any] = {
        "listing_id": made.listing_id,
        "marketplace": "amazon",
        "source": source,
        "field_path": field,
        "error_code": code,
        "error_class": "content_error",
        "retry_class": "user_action",
        "severity": "warning",
        "message": "The title uses a word the brand avoids.",
        "dedupe_key": f"{source}|{field}|{code}|",
        "listing_version_id": made.version_id,
    }
    tenant = world.scalar("SELECT tenant_id FROM listings WHERE id = %s", [made.listing_id])
    async with tenant_uow(tenant) as uow:
        row, _ = await uow.listing_issues.upsert_open(NewIssue(**{**values, **over}))
        return row.id
