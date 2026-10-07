"""S14b T3 (AC 1; D2): the stub scenarios over an in-memory `FakeDeps` that records every call."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from mkos_pipeline import run_listing_pipeline
from mkos_pipeline.graph import LONG_TITLE_LENGTH, STUB_EPOCH, scenario
from mkos_schemas.codes import ERROR_CODE_META, ErrorCode
from mkos_schemas.enums import ErrorSource, PipelineStage, RetryClass
from mkos_schemas.errors import ErrorItem, ErrorReport
from mkos_schemas.images import ImageReport
from mkos_schemas.jobs import BrandChunkDTO, GenerationContext, GenerationResult, ModelCredentials
from mkos_schemas.listing import CanonicalListing

FIXTURES = Path(__file__).resolve().parents[2] / "schemas" / "fixtures"
TITLE_LIMIT = 200
PORTS = (
    "retrieve_brand_chunks",
    "validate_rules",
    "validate_images",
    "get_model_credentials",
    "emit_node_event",
)


class FakeDeps:
    """Records every port call. `validate_rules` fails a title over 200 chars (FIELD_TOO_LONG)."""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raise_on = raise_on

    def _hit(self, port: str, *args: Any) -> None:
        self.calls.append((port, args))
        if port == self.raise_on:
            raise RuntimeError(f"seeded {port} failure")

    async def retrieve_brand_chunks(
        self, tenant_id: UUID, query: str, k: int = 8, types: Sequence[str] | None = None
    ) -> Sequence[BrandChunkDTO]:
        self._hit("retrieve_brand_chunks", tenant_id, query, k, types)
        return []

    async def validate_rules(self, listing: CanonicalListing) -> ErrorReport:
        self._hit("validate_rules", listing)
        if len(listing.content.title) <= TITLE_LIMIT:
            return ErrorReport(status="passed", listing_id=listing.listing_id)
        meta = ERROR_CODE_META[ErrorCode.FIELD_TOO_LONG]
        item = ErrorItem(
            field="content.title",
            error_code=ErrorCode.FIELD_TOO_LONG,
            message="Title too long",
            source=ErrorSource.RULES_ENGINE,
            retryable=meta.retry_class in (RetryClass.AUTO_RETRY, RetryClass.AI_CORRECT),
            retry_class=meta.retry_class,
        )
        return ErrorReport(status="failed", errors=[item], listing_id=listing.listing_id)

    async def validate_images(self, listing: CanonicalListing) -> list[ImageReport]:
        self._hit("validate_images", listing)
        return []

    async def get_model_credentials(
        self, purpose: Literal["generation", "vision", "embedding"]
    ) -> ModelCredentials:
        self._hit("get_model_credentials", purpose)
        return ModelCredentials(provider="openai", api_key="local_only_model_key_unset", model="m")

    async def emit_node_event(
        self,
        node: PipelineStage,
        status: Literal["started", "ok", "failed"],
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._hit("emit_node_event", node, status, detail)

    def ports(self) -> set[str]:
        return {name for name, _ in self.calls}

    def nodes(self) -> list[tuple[str, str]]:
        return [(str(a[0]), str(a[1])) for n, a in self.calls if n == "emit_node_event"]


def _ctx(**over: Any) -> GenerationContext:
    data = json.loads((FIXTURES / "GenerationContext.json").read_text(encoding="utf-8"))
    data.update(over)
    return GenerationContext.model_validate(data)


def _previous(ctx: GenerationContext, count: int) -> dict[str, Any]:
    slots = ["main", "secondary", "lifestyle"]
    return {
        "listing_id": str(ctx.listing_id),
        "product_id": str(ctx.product_id),
        "marketplace": ctx.marketplace.value,
        "category": ctx.category,
        "content": {"title": "Old title", "bullets": ["b"], "description": "d"},
        "images": [
            {"image_id": f"00000000-0000-0000-0000-00000000000{i + 1}", "slot": slots[i]}
            for i in range(count)
        ],
    }


async def _run(ctx: GenerationContext, deps: FakeDeps | None = None) -> GenerationResult:
    return await run_listing_pipeline(ctx, deps or FakeDeps())


async def test_stub_scenario_ok_generated() -> None:
    result = await _run(_ctx())
    assert result.terminal_state == "generated"
    assert result.listing is not None and len(result.listing.content.title) == 60
    assert result.attempts_used == 1 and result.error is None
    assert result.validation is not None and result.validation.passed
    assert [s.source for s in result.validation.stages] == [ErrorSource.RULES_ENGINE]
    assert all(b.status == "ok" for b in result.blocks)


async def test_stub_scenario_blocked_three_attempts() -> None:
    deps = FakeDeps()
    result = await _run(_ctx(user_instructions="stub:blocked"), deps)
    assert result.terminal_state == "blocked"
    assert result.attempts_used == 3
    assert result.listing is not None and len(result.listing.content.title) == LONG_TITLE_LENGTH
    assert result.error is not None
    assert [e.error_code for e in result.error.errors] == [ErrorCode.FIELD_TOO_LONG]
    assert result.error.attempts_used == 3
    assert [n for n, _ in deps.calls].count("validate_rules") == 3
    corrections = [r for r in result.agent_runs if r.agent_name == "correction"]
    assert [r.attempt for r in corrections] == [2, 3]


@pytest.mark.parametrize("cap", [1, 2, 3])
async def test_stub_scenario_blocked_respects_max_attempts(cap: int) -> None:
    deps = FakeDeps()
    result = await _run(_ctx(user_instructions="stub:blocked", max_attempts=cap), deps)
    assert result.terminal_state == "blocked"
    assert result.attempts_used == cap
    assert [n for n, _ in deps.calls].count("validate_rules") == cap
    assert result.validation is not None and len(result.validation.stages) == cap


async def test_stub_scenario_failed() -> None:
    result = await _run(_ctx(user_instructions="stub:failed"))
    assert result.listing is None
    assert result.terminal_state == "failed"
    assert result.error is not None and result.error.error_code is ErrorCode.GENERATION_FAILED
    assert result.blocks and all(b.status == "failed" for b in result.blocks)


async def test_stub_unknown_scenario_is_ok() -> None:
    for text in ("stub:nope", "stub:", "make it pop", None):
        ctx = _ctx(user_instructions=text)
        assert scenario(ctx) == "ok"
        assert (await _run(ctx)).terminal_state == "generated"


def _vision_ctx(count: int) -> GenerationContext:
    base = _ctx(user_instructions="stub:vision_warn")
    return _ctx(user_instructions="stub:vision_warn", previous_version=_previous(base, count))


@pytest.mark.parametrize("name", ["ok", "blocked", "failed", "vision_warn"])
async def test_stub_result_validates(name: str) -> None:
    ctx = _vision_ctx(2) if name == "vision_warn" else _ctx(user_instructions=f"stub:{name}")
    result = await _run(ctx)
    again = GenerationResult.model_validate_json(result.model_dump_json(by_alias=True))
    assert again == result


async def test_stub_scenario_vision_warn() -> None:
    ctx = _vision_ctx(2)
    result = await _run(ctx)
    assert result.terminal_state == "generated"
    assert result.listing is not None
    ids = [i.image_id for i in result.listing.images]
    assert ids == [i.image_id for i in ctx.previous_version.images]  # type: ignore[union-attr]
    assert [r.image_id for r in result.image_reports] == ids
    image_run = next(r.id for r in result.agent_runs if r.agent_name == "image")
    for report in result.image_reports:
        assert report.pass_ is True
        assert [(c.check, c.method, c.passed) for c in report.checks] == [
            ("text_overlay", "vision", False)
        ]
        assert [(i.code.value, i.check) for i in report.issues] == [
            ("TEXT_DETECTED", "text_overlay")
        ]
        assert report.agent_run_id == image_run
        assert report.processed_at == STUB_EPOCH
        assert report.marketplace is ctx.marketplace


async def test_stub_scenario_vision_warn_no_images_empty() -> None:
    result = await _run(_ctx(user_instructions="stub:vision_warn"))
    assert result.image_reports == []
    assert result.terminal_state == "generated"


async def test_stub_scenario_ok_has_no_image_reports() -> None:
    base = _ctx()
    result = await _run(_ctx(previous_version=_previous(base, 2)))
    assert result.image_reports == []


async def test_stub_byte_identical_across_runs() -> None:
    for name in ("ok", "blocked", "failed"):
        ctx = _ctx(user_instructions=f"stub:{name}")
        assert (await _run(ctx)).model_dump_json() == (await _run(ctx)).model_dump_json()
    ctx = _vision_ctx(2)
    assert (await _run(ctx)).model_dump_json() == (await _run(ctx)).model_dump_json()


@pytest.mark.parametrize("name", ["ok", "blocked", "vision_warn"])
async def test_stub_calls_all_five_ports(name: str) -> None:
    deps = FakeDeps()
    await _run(_ctx(user_instructions=f"stub:{name}"), deps)
    assert deps.ports() == set(PORTS)


async def test_stub_calls_brand_chunks_for_own_tenant() -> None:
    deps = FakeDeps()
    ctx = _ctx()
    await _run(ctx, deps)
    (args,) = [a for n, a in deps.calls if n == "retrieve_brand_chunks"]
    assert args[0] == ctx.tenant_id and args[2] == 8


async def test_stub_last_node_quality_check() -> None:
    deps = FakeDeps()
    await _run(_ctx(), deps)
    assert deps.nodes()[-1] == ("quality_check", "started")


@pytest.mark.parametrize("name", ["ok", "blocked", "failed", "vision_warn"])
async def test_stub_no_publish_or_approval_node(name: str) -> None:
    deps = FakeDeps()
    await _run(_ctx(user_instructions=f"stub:{name}"), deps)
    stages = {s.value for s in PipelineStage}
    for node, status in deps.nodes():
        assert node in stages
        assert status in {"started", "ok", "failed"}
        assert "publish" not in node and "approv" not in node


@pytest.mark.parametrize("port", PORTS)
async def test_stub_port_exception_propagates(port: str) -> None:
    with pytest.raises(RuntimeError, match=f"seeded {port} failure"):
        await _run(_ctx(), FakeDeps(raise_on=port))


async def test_stub_ids_stable_per_job() -> None:
    one = await _run(_ctx())
    other = await _run(_ctx(job_id="55555555-5555-5555-5555-555555555555"))
    assert {r.id for r in one.agent_runs}.isdisjoint({r.id for r in other.agent_runs})
    again = await _run(_ctx())
    assert [r.id for r in one.agent_runs] == [r.id for r in again.agent_runs]


async def test_stub_records_are_free_and_stub_model() -> None:
    result = await _run(_ctx(user_instructions="stub:blocked"))
    names = [r.agent_name for r in result.agent_runs]
    assert names == ["content", "keyword", "attribute", "image", "correction", "correction"]
    for record in result.agent_runs:
        assert record.cost_usd == 0 and record.tokens.input == 0 and record.tokens.output == 0
        assert record.model_used == "stub"
        assert record.started_at == record.created_at == STUB_EPOCH


async def test_stub_fills_required_attribute_rules() -> None:
    rules = json.loads((FIXTURES / "GenerationContext.json").read_text(encoding="utf-8"))[
        "marketplace_rules"
    ]
    rules["rules"] = [
        {
            "id": "00000000-0000-0000-0000-0000000000a1",
            "marketplace": "amazon",
            "category": "apparel",
            "field": "attributes.color",
            "rule_def": {"type": "required_attribute", "attribute": "color", "message": "m"},
        },
        {
            "id": "00000000-0000-0000-0000-0000000000a2",
            "marketplace": "amazon",
            "category": "apparel",
            "field": "attributes.size",
            "rule_def": {
                "type": "required_attribute",
                "attribute": "size",
                "allowed_values": ["m", "l"],
                "message": "m",
            },
        },
    ]
    template = {
        "marketplace": "amazon",
        "category": "apparel",
        "required_attributes": ["fit", "pieces", "lined", "chest"],
        "specs": {
            "fit": {"requirement": "required", "value_type": "enum", "allowed_values": ["slim"]},
            "pieces": {"requirement": "required", "value_type": "number"},
            "lined": {"requirement": "required", "value_type": "boolean"},
            "chest": {"requirement": "required", "value_type": "measurement", "unit": "cm"},
        },
    }
    result = await _run(_ctx(marketplace_rules=rules, category_template=template))
    assert result.listing is not None
    assert result.listing.attributes == {
        "chest": "1 cm",
        "color": "stub",
        "fit": "slim",
        "lined": True,
        "pieces": 1,
        "size": "m",
    }
