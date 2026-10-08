"""S18 T12 (AC 3, 16; D8; CP13): the `validate_rules` port-call cap and recorder, the product facts it
evaluates against, and one span per pipeline node."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from .auth_support import World
from .conftest import LOCAL_HOSTS
from .seam_support import CATEGORY, Conn, add_rule, running_job
from .test_pipeline_ports_a import build
from .test_pipeline_ports_b import listing, loaded

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def spans() -> Iterator[Any]:
    from app.services import pipeline_deps
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    saved = pipeline_deps._tracer
    pipeline_deps.use_tracer(provider.get_tracer("app"))
    yield exporter
    pipeline_deps.use_tracer(saved)


async def test_validate_rules_port_call_cap(products_world: World, auth_db: Conn) -> None:
    from mkos_schemas.codes import ErrorCode

    deps = await build(products_world, auth_db, rules=await loaded(products_world))
    draft = listing(uuid.uuid4())
    for _ in range(3):
        assert (await deps.validate_rules(draft)).passed
    fourth = await deps.validate_rules(draft)
    assert (fourth.passed, fourth.error_code) == (False, ErrorCode.INTERNAL_ERROR)
    assert len(deps.rules_calls) == 3 and deps.validate_calls == 4


async def test_validate_rules_recorder_attempts_1_2_3(products_world: World, auth_db: Conn) -> None:
    deps = await build(products_world, auth_db, rules=await loaded(products_world))
    for title in ("one", "two", "three"):
        await deps.validate_rules(listing(uuid.uuid4(), title=f"A short {title} title"))
    assert [c.attempt for c in deps.rules_calls] == [1, 2, 3]
    assert all(c.duration_ms >= 0 and c.report.passed for c in deps.rules_calls)
    with pytest.raises(AttributeError):
        deps.rules_calls.append(None)


async def test_validate_rules_uses_product_facts(products_world: World, auth_db: Conn) -> None:
    """A `product.*` conditional rule fires on the `for_rules` mapping the deps hold."""
    from app.core.resolved_facts import Identity, ProductFacts, for_rules

    from . import factories as f

    add_rule(
        auth_db,
        {
            "type": "conditional_required",
            "attribute": "fabric_type",
            "when": [{"path": "product.level", "op": "eq", "value": "style"}],
            "message": "Fabric is required for a style.",
            "policy_ref": f.POLICY_REF,
        },
        field="attributes.fabric_type",
        subject="fabric_type",
    )
    rules = await loaded(products_world)
    facts = ProductFacts(
        product_id=uuid.uuid4(), sku="S-1", level="style", identity=Identity(brand_name="Acme")
    )
    deps = await build(products_world, auth_db, rules=rules, product=for_rules(facts))
    assert not (await deps.validate_rules(listing(uuid.uuid4()))).passed
    other = await build(
        products_world,
        auth_db,
        rules=rules,
        product=for_rules(facts.model_copy(update={"level": "standalone"})),
    )
    assert (await other.validate_rules(listing(uuid.uuid4()))).passed


async def test_node_spans_one_per_node(products_world: World, auth_db: Conn, spans: Any) -> None:
    from mkos_schemas.enums import PipelineStage

    acme = products_world.tenant("acme")
    job = running_job(auth_db, acme)
    deps = await build(products_world, auth_db, job_id=job)
    for node in (PipelineStage.LOAD_CONTEXT, PipelineStage.CONTENT_AGENT):
        await deps.emit_node_event(node, "started")
        await deps.emit_node_event(node, "ok", {"secret_detail": "do-not-export"})
    await deps.emit_node_event(PipelineStage.VALIDATE_RULES, "started")
    await deps.emit_node_event(PipelineStage.VALIDATE_RULES, "failed")
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == [
        "pipeline.load_context",
        "pipeline.content_agent",
        "pipeline.validate_rules",
    ]
    for span in finished:
        assert set(span.attributes) == {"job_id", "tenant_id", "node"}
        assert span.attributes["job_id"] == str(job)
        assert "do-not-export" not in repr(dict(span.attributes))
    assert [s.status.status_code.name for s in finished] == ["OK", "OK", "ERROR"]


async def test_close_spans_ends_open(products_world: World, auth_db: Conn, spans: Any) -> None:
    from mkos_schemas.enums import PipelineStage

    job = running_job(auth_db, products_world.tenant("acme"))
    deps = await build(products_world, auth_db, job_id=job)
    await deps.emit_node_event(PipelineStage.MERGE_OUTPUTS, "started")
    assert spans.get_finished_spans() == ()
    deps.close_spans()
    deps.close_spans()  # idempotent
    [span] = spans.get_finished_spans()
    assert (span.name, span.status.status_code.name) == ("pipeline.merge_outputs", "ERROR")


def test_category_constant() -> None:
    assert CATEGORY == "ethnic_wear_kurta_sets"
