"""S14b T8 (AC 3; D6, D7, C2): `validate_rules`, `validate_images` and `emit_node_event` on
`BackendPipelineDeps`."""

from __future__ import annotations

import ast
import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .seam_support import (
    CATEGORY,
    Conn,
    add_image,
    add_product,
    add_rule,
    max_length,
    node_of,
    running_job,
    terminal_job,
)
from .test_pipeline_ports_a import build, settings

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
MODULE = Path(__file__).resolve().parents[2] / "app" / "services" / "pipeline_deps.py"


def listing(product_id: uuid.UUID, *, title: str = "A short kurta title", **over: Any) -> Any:
    from mkos_schemas.listing import CanonicalListing

    data: dict[str, Any] = {
        "listing_id": str(uuid.uuid4()),
        "product_id": str(product_id),
        "marketplace": "amazon",
        "category": CATEGORY,
        "content": {"title": title, "bullets": ["one"], "description": "d"},
        **over,
    }
    return CanonicalListing.model_validate(data)


async def loaded(world: World, slug: str = "acme") -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.rules_loader import load_rules

    async with tenant_uow(world.tenant(slug)) as uow:
        return await load_rules(
            uow, tenant_id=world.tenant(slug), marketplace="amazon", category=CATEGORY
        )


def _is_internal(report: Any) -> bool:
    from mkos_schemas.codes import ErrorCode
    from mkos_schemas.enums import ErrorSource

    (item,) = report.errors
    return (
        report.status == "failed"
        and not report.passed
        and report.error_code is ErrorCode.INTERNAL_ERROR
        and (item.field, item.source, item.retryable) == ("listing", ErrorSource.SYSTEM, False)
    )


# ---- validate_rules ----


async def test_port_validate_rules_equals_evaluate(products_world: World, auth_db: Conn) -> None:
    from app.services.rules_engine import evaluate

    add_rule(auth_db, max_length(20))
    rules = await loaded(products_world)
    deps = await build(products_world, auth_db, rules=rules)
    for title in ("short", "a title well over twenty characters"):
        draft = listing(uuid.uuid4(), title=title)
        report = await deps.validate_rules(draft)
        assert report.model_dump_json() == evaluate(draft, rules.ruleset).model_dump_json()
    assert not report.passed


async def test_port_validate_rules_other_tenant_ruleset_internal_error(
    products_world: World, auth_db: Conn
) -> None:
    from app.services.rules_loader import LoadedRules
    from mkos_schemas.rules import RuleSet

    foreign = {
        "id": str(uuid.uuid4()),
        "tenant_id": str(products_world.tenant("bharat")),
        "marketplace": "amazon",
        "category": CATEGORY,
        "field": "content.title",
        "rule_def": {"type": "banned_terms", "terms": ["zzz"], "message": "m"},
    }
    ruleset = RuleSet.model_validate(
        {
            "marketplace": "amazon",
            "category": CATEGORY,
            "rules": [foreign],
            "loaded_at": datetime.now(UTC),
        }
    )
    deps = await build(products_world, auth_db, rules=LoadedRules(ruleset=ruleset, template=None))
    assert _is_internal(await deps.validate_rules(listing(uuid.uuid4())))


async def test_port_validate_rules_pair_mismatch_internal_error(
    products_world: World, auth_db: Conn
) -> None:
    deps = await build(products_world, auth_db, rules=await loaded(products_world))
    assert _is_internal(await deps.validate_rules(listing(uuid.uuid4(), category="home_decor")))
    assert _is_internal(await deps.validate_rules(listing(uuid.uuid4(), marketplace="flipkart")))


async def test_port_validate_rules_engine_exception_internal_error(
    products_world: World, auth_db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import pipeline_deps

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("engine exploded with a secret-ish value 4242")

    deps = await build(products_world, auth_db, rules=await loaded(products_world))
    monkeypatch.setattr(pipeline_deps, "evaluate", boom)
    report = await deps.validate_rules(listing(uuid.uuid4()))
    assert _is_internal(report)
    assert "4242" not in report.model_dump_json()


# ---- validate_images ----


async def test_port_validate_images_stored_reports_only(
    products_world: World, auth_db: Conn
) -> None:
    tenant = products_world.tenant("acme")
    product = add_product(auth_db, tenant)
    first = add_image(auth_db, tenant, product, "amazon")
    second = add_image(auth_db, tenant, product, "amazon", slot="lifestyle")
    deps = await build(products_world, auth_db, product_id=product)
    refs = [
        {"image_id": str(second), "slot": "lifestyle"},
        {"image_id": str(uuid.uuid4()), "slot": "secondary"},
        {"image_id": str(first), "slot": "secondary"},
    ]
    reports = await deps.validate_images(listing(product, images=refs))
    assert [r.image_id for r in reports] == [second, first]


async def test_port_validate_images_other_marketplace_empty(
    products_world: World, auth_db: Conn
) -> None:
    tenant = products_world.tenant("acme")
    product = add_product(auth_db, tenant)
    image = add_image(auth_db, tenant, product, "amazon", "flipkart")
    deps = await build(products_world, auth_db, product_id=product)
    refs = [{"image_id": str(image), "slot": "secondary"}]
    assert await deps.validate_images(listing(product, images=refs, marketplace="flipkart")) == []


async def test_port_validate_images_unchecked_omitted(products_world: World, auth_db: Conn) -> None:
    tenant = products_world.tenant("acme")
    product = add_product(auth_db, tenant)
    unchecked = add_image(auth_db, tenant, product)
    deps = await build(products_world, auth_db, product_id=product)
    refs = [{"image_id": str(unchecked), "slot": "secondary"}]
    assert await deps.validate_images(listing(product, images=refs)) == []


async def test_port_validate_images_other_tenant_never_loaded(
    products_world: World, auth_db: Conn
) -> None:
    bharat = products_world.tenant("bharat")
    product = add_product(auth_db, bharat)
    image = add_image(auth_db, bharat, product, "amazon")
    deps = await build(products_world, auth_db, product_id=product)  # acme's deps
    refs = [{"image_id": str(image), "slot": "secondary"}]
    assert await deps.validate_images(listing(product, images=refs)) == []


def test_port_validate_images_no_model_import() -> None:
    banned = {"openai", "httpx", "socket", "PIL", "app.services.vision", "requests"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    assert {m for m in found if m.split(".")[0] in banned or m in banned} == set()


# ---- emit_node_event ----


async def _deps_for_job(world: World, conn: Conn, job: uuid.UUID) -> Any:
    return await build(world, conn, job_id=job)


async def test_port_emit_node_event_sets_node_running_only(
    products_world: World, auth_db: Conn
) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    await deps.emit_node_event(PipelineStage.RETRIEVE_BRAND, "started")
    assert node_of(auth_db, job) == "retrieve_brand"
    await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "ok")
    await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "failed", {"why": "x"})
    assert node_of(auth_db, job) == "retrieve_brand"
    row = auth_db.execute("SELECT status FROM listings").fetchall()
    assert {r[0] for r in row} == {"created"}  # never listings.status (BA:1536)


async def test_port_emit_node_event_other_job_untouched(
    products_world: World, auth_db: Conn
) -> None:
    from mkos_schemas.enums import PipelineStage

    tenant = products_world.tenant("acme")
    mine, other = running_job(auth_db, tenant), running_job(auth_db, tenant)
    deps = await _deps_for_job(products_world, auth_db, mine)
    await deps.emit_node_event(PipelineStage.MERGE_OUTPUTS, "started")
    assert (node_of(auth_db, mine), node_of(auth_db, other)) == ("merge_outputs", None)


async def test_port_emit_node_event_terminal_job_untouched(
    products_world: World, auth_db: Conn, log_lines: Any
) -> None:
    from mkos_schemas.enums import PipelineStage

    job = terminal_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    capture = log_lines()
    await deps.emit_node_event(PipelineStage.QUALITY_CHECK, "started")
    assert node_of(auth_db, job) is None
    assert "seam.node_event_dropped" in [line["message"] for line in capture.lines()]


@pytest.mark.parametrize(("node", "status"), [("bogus", "started"), ("correct", "done")])
async def test_port_emit_node_event_bad_args_raise(
    products_world: World, auth_db: Conn, node: str, status: str
) -> None:
    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    with pytest.raises(ValueError, match=r"node|status"):
        await deps.emit_node_event(node, status)
    assert node_of(auth_db, job) is None


async def test_port_emit_node_event_detail_not_persisted(
    products_world: World, auth_db: Conn
) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    marker = f"detail-marker-{uuid.uuid4().hex}"
    await deps.emit_node_event(PipelineStage.CORRECT, "started", {"note": marker})
    row = auth_db.execute("SELECT j::text FROM job_runs j WHERE id = %s", [job]).fetchone()
    assert row is not None and marker not in row[0]


async def test_port_emit_node_event_detail_redacted_and_capped(
    products_world: World, auth_db: Conn, log_lines: Any
) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    capture = log_lines()
    secret = "-".join(["sk", "proj", uuid.uuid4().hex])  # built at run time, never committed
    await deps.emit_node_event(
        PipelineStage.CORRECT, "ok", {"api_key": secret, "blob": "x" * 10_240}
    )
    (line,) = [entry for entry in capture.lines() if entry["message"] == "seam.node_event"]
    assert len(line["detail"].encode("utf-8")) <= 2048
    assert secret not in capture.text()


async def test_port_emit_node_event_short_transaction(products_world: World, auth_db: Conn) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "started")
    await deps.emit_node_event(PipelineStage.KEYWORD_AGENT, "started")
    idle = auth_db.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND state LIKE 'idle in transaction%%' AND usename = 'app_rw'"
    ).fetchone()
    assert idle == (0,)


async def test_port_emit_node_event_concurrent_idempotent(
    products_world: World, auth_db: Conn
) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await _deps_for_job(products_world, auth_db, job)
    await asyncio.gather(
        *(deps.emit_node_event(PipelineStage.VALIDATE_RULES, "started") for _ in range(10))
    )
    assert node_of(auth_db, job) == "validate_rules"


def test_port_settings_helper_is_local() -> None:
    assert settings().app_env == "local"
