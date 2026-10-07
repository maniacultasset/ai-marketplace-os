"""S14b T13 (AC 3, 8, 9; CANON.md:137): the seam ports and the rules API as `app_rw` on `mos_iso`.
`acme` and `bharat` each hold rows of every kind; neither ever reads, changes or is judged by the
other's."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import psycopg
import pytest
from tests.integration import seam_support as seam
from tests.integration.auth_support import World, bearer, build_world, tokens_for

pytestmark = pytest.mark.usefixtures("iso_settings")
Conn = psycopg.Connection[tuple[object, ...]]
RULES = "/api/v1/rules"
DRY_RUN = "/api/v1/rules/dry-run"


@pytest.fixture
def world(clean_tenants: None, admin_conn: Conn) -> World:
    return build_world(admin_conn)


@pytest.fixture
async def api(world: World) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import create_app

    transport = httpx.ASGITransport(app=create_app(), client=("203.0.113.9", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _as(world: World, slug: str, role: str = "operator") -> dict[str, str]:
    return bearer(tokens_for(world, f"{role}@{slug}.test", slug))


def _draft(title: str) -> dict[str, Any]:
    return {
        "listing": {
            "product_id": str(uuid.uuid4()),
            "marketplace": "amazon",
            "category": seam.CATEGORY,
            "content": {"title": title, "bullets": ["b"], "description": "d"},
        }
    }


async def test_iso_rules_list_bharat_lacks_acme_rules(
    api: httpx.AsyncClient, world: World, admin_conn: Conn
) -> None:
    acme_rule = seam.add_rule(admin_conn, seam.banned(["cheap"]), tenant_id=world.tenant("acme"))
    bharat_rule = seam.add_rule(admin_conn, seam.banned(["dear"]), tenant_id=world.tenant("bharat"))
    response = await api.get(RULES, params={"limit": 200}, headers=_as(world, "bharat"))
    assert response.status_code == 200, response.text
    ids = {r["id"] for r in response.json()["data"]}
    assert str(bharat_rule) in ids and str(acme_rule) not in ids


async def test_iso_dry_run_acme_term_not_applied_to_bharat(
    api: httpx.AsyncClient, world: World, admin_conn: Conn
) -> None:
    seam.add_template(admin_conn)
    seam.add_rule(admin_conn, seam.banned(["handloom"]), tenant_id=world.tenant("acme"))
    acme = await api.post(DRY_RUN, json=_draft("Handloom kurta"), headers=_as(world, "acme"))
    bharat = await api.post(DRY_RUN, json=_draft("Handloom kurta"), headers=_as(world, "bharat"))
    assert acme.status_code == bharat.status_code == 200, (acme.text, bharat.text)
    assert acme.json()["passed"] is False
    assert bharat.json()["passed"] is True


async def _deps(world: World, conn: Conn, slug: str, **over: Any) -> Any:
    from datetime import UTC, datetime

    from app.core.config import get_settings
    from app.services.pipeline_deps import build_pipeline_deps
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.rules import RuleSet

    tenant = world.tenant(slug)
    ruleset = RuleSet.model_validate(
        {"marketplace": "amazon", "category": seam.CATEGORY, "loaded_at": datetime.now(UTC)}
    )
    values: dict[str, Any] = {
        "settings": get_settings(),
        "tenant_id": tenant,
        "job_id": uuid.uuid4(),
        "user_id": world.user(f"owner@{slug}.test"),
        "product_id": seam.add_product(conn, tenant),
        "marketplace": "amazon",
        "rules": LoadedRules(ruleset=ruleset, template=None),
        **over,
    }
    return await build_pipeline_deps(**values)


def _chunk(conn: Conn, tenant: uuid.UUID, text: str) -> uuid.UUID:
    from app.core.vectors import hash_vector
    from tests.integration import factories as f

    vector = "[" + ",".join(repr(v) for v in hash_vector(text)) + "]"
    return f.brand_chunk(conn, tenant, text=text, embedding=vector, source_type="brand_profile")


async def test_iso_retrieve_brand_chunks_acme_only(world: World, admin_conn: Conn) -> None:
    from app.services.pipeline_deps import SeamPortError

    acme, bharat = world.tenant("acme"), world.tenant("bharat")
    mine = {_chunk(admin_conn, acme, f"acme tone {n}") for n in range(3)}
    _chunk(admin_conn, bharat, "bharat tone")
    deps = await _deps(world, admin_conn, "acme")
    found = await deps.retrieve_brand_chunks(acme, "tone", k=50)
    assert {c.id for c in found} == mine
    with pytest.raises(SeamPortError):
        await deps.retrieve_brand_chunks(bharat, "tone")


async def test_iso_set_node_cross_tenant_false(world: World, admin_conn: Conn) -> None:
    from mkos_schemas.enums import PipelineStage

    theirs = seam.running_job(admin_conn, world.tenant("bharat"))
    deps = await _deps(world, admin_conn, "acme", job_id=theirs)
    await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "started")
    assert seam.node_of(admin_conn, theirs) is None
    from app.repositories.uow import tenant_uow

    async with tenant_uow(world.tenant("acme")) as uow:
        assert await uow.job_runs.set_node(theirs, "content_agent") is False


async def test_iso_image_reports_cross_tenant_empty(world: World, admin_conn: Conn) -> None:
    bharat = world.tenant("bharat")
    product = seam.add_product(admin_conn, bharat)
    image = seam.add_image(admin_conn, bharat, product, "amazon")
    deps = await _deps(world, admin_conn, "acme", product_id=product)
    from mkos_schemas.listing import CanonicalListing

    listing = CanonicalListing.model_validate(
        {
            "product_id": str(product),
            "marketplace": "amazon",
            "category": seam.CATEGORY,
            "content": {"title": "t", "bullets": ["b"], "description": "d"},
            "images": [{"image_id": str(image), "slot": "secondary"}],
        }
    )
    assert await deps.validate_images(listing) == []
