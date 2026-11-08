"""SI12 T22 (AC13): the critical seller fields never take an AI value, and neither a size-chart
read nor an HSN suggestion reaches the generation facts."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.core.errors import InputError
from app.repositories.uow import tenant_uow
from mkos_schemas.codes import ErrorCode

from . import factories as f
from .auth_support import World
from .conftest import LOCAL_HOSTS

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)

STAMP = "2026-11-08T00:00:00Z"
AI = {
    "source": "ai",
    "captured_via": "agent",
    "evidence_ref": "image:8a3f5c2e-1d4b-4e6a-9b7c-0d2e4f6a8b13",
    "confidence": 0.9,
    "agent_run_id": "00000000-0000-0000-0000-000000000001",
    "updated_at": STAMP,
}
SELLER = {"source": "seller", "captured_via": "api", "updated_at": STAMP}
TYPED: list[tuple[str, dict[str, Any]]] = [
    ("hsn_code", {"hsn_code": "6109"}),
    ("country_of_origin", {"country_of_origin": "IN"}),
    (
        "package_dimensions",
        {
            "package_length_cm": Decimal("30"),
            "package_breadth_cm": Decimal("20"),
            "package_height_cm": Decimal("4"),
        },
    ),
    ("package_weight", {"package_weight_g": 450}),
]
CHART = {"unit": "cm", "basis": "garment", "rows": [{"size": "M", "chest": 100}]}


@pytest.mark.parametrize(("key", "values"), TYPED, ids=[k for k, _ in TYPED])
async def test_ai_source_refused_on_the_typed_fields(
    products_world: World, key: str, values: dict[str, Any]
) -> None:
    acme = products_world.tenant("acme")
    p = f.product(products_world.conn, acme)
    with pytest.raises(InputError) as caught:
        async with tenant_uow(acme) as uow:
            await uow.facts.set_typed_group(p, values, AI)
    assert caught.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN
    # guard-must-fail: the same write as the seller passes
    async with tenant_uow(acme) as uow:
        assert await uow.facts.set_typed_group(p, values, SELLER)


async def test_ai_source_refused_on_the_size_chart(products_world: World) -> None:
    acme = products_world.tenant("acme")
    p = f.product(products_world.conn, acme)
    with pytest.raises(InputError) as caught:
        async with tenant_uow(acme) as uow:
            await uow.facts.set_attribute(p, "size_measurements", {"normalized": CHART, **AI})
    assert caught.value.code is ErrorCode.FACT_SOURCE_FORBIDDEN
    async with tenant_uow(acme) as uow:
        assert await uow.facts.set_attribute(
            p, "size_measurements", {"normalized": CHART, **SELLER}
        )


async def test_read_result_and_hsn_suggestion_never_reach_the_facts(
    products_world: World,
) -> None:
    acme = products_world.tenant("acme")
    p = f.product(products_world.conn, acme, category="tshirts")
    image = uuid.uuid4()
    read = {
        "status": "read",
        "source_hash": "d" * 64,
        "job_id": str(uuid.uuid4()),
        "chart": {"unit": "cm", "basis": "garment", "rows": [{"size": "ZZREAD", "chest": 99}]},
    }
    f.image(
        products_world.conn,
        acme,
        p,
        id=image,
        slot="size_chart",
        storage_key=f"{acme}/{p}/images/{image}",
        clean_content_hash="d" * 64,
        clean_mime_type="image/jpeg",
        metadata={"size_chart_read": read},
    )
    async with tenant_uow(acme) as uow:
        facts = await uow.product_facts.resolve(p)
    assert facts is not None
    dumped = facts.model_dump_json() if hasattr(facts, "model_dump_json") else repr(facts)
    assert "ZZREAD" not in dumped
    assert "6109" not in dumped  # the tshirts HSN heading is only a suggestion
    stored = products_world.conn.execute(
        "SELECT attributes, provenance, hsn_code FROM products WHERE id = %s", [p]
    ).fetchone()
    assert stored is not None
    assert "size_measurements" not in stored[0] and stored[2] is None
