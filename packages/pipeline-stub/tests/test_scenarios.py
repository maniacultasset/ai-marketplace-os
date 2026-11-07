"""S14b T3 (AC 1; D2): the stub scenarios over an in-memory `FakeDeps` that records every call."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from mkos_pipeline import run_listing_pipeline
from mkos_pipeline.graph import (
    FIXABLE_TITLE_LENGTH,
    LONG_TITLE_LENGTH,
    SCENARIOS,
    STUB_EPOCH,
    scenario,
)
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


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("credit", ErrorCode.AI_CREDIT_EXHAUSTED),
        ("credit_legacy", ErrorCode.BUDGET_EXCEEDED),
        ("key_refused", ErrorCode.AI_PROVIDER_KEY_INVALID),
    ],
)
async def test_stub_scenario_account_refusals(name: str, code: ErrorCode) -> None:
    """SI10: one failed content record with the refusal code, no listing, USD 0."""
    assert name in SCENARIOS
    result = await _run(_ctx(user_instructions=f"stub:{name}"))
    assert result.listing is None
    assert result.terminal_state == "failed"
    [record] = result.agent_runs
    assert (record.agent_name, record.status) == ("content", "failed")
    assert record.cost_usd == 0
    assert record.error_report is not None and record.error_report.error_code is code
    again = GenerationResult.model_validate_json(result.model_dump_json(by_alias=True))
    assert again == result


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


async def test_stub_scenario_warnings_generated() -> None:
    """S19 T22: the stub only puts the word in the title; the engine decides the warning."""
    ctx = _ctx(user_instructions="stub:warnings")
    assert scenario(ctx) == "warnings"
    result = await _run(ctx)
    assert result.terminal_state == "generated"
    assert result.listing is not None and "stubwarn" in result.listing.content.title
    assert result.image_reports == []


async def test_stub_byte_identical_across_runs_warnings() -> None:
    ctx = _ctx(user_instructions="stub:warnings")
    assert (await _run(ctx)).model_dump_json() == (await _run(ctx)).model_dump_json()


# ---- S26 T9 (AC 2 API10, AC 5): the failure-injection scenarios ----


async def test_stub_timeout_sleeps_past_ctx_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    deps = FakeDeps()
    with pytest.raises(TimeoutError):
        await run_listing_pipeline(_ctx(user_instructions="stub:timeout"), deps)
    credentials = ModelCredentials(
        provider="openai", api_key="local_only_model_key_unset", model="m"
    )
    assert slept == [credentials.timeout_seconds + 1]
    # it loaded the context and the brand chunks, then stopped before any validation
    assert {"get_model_credentials", "retrieve_brand_chunks"} <= deps.ports()
    assert "validate_rules" not in deps.ports()
    assert scenario(_ctx(user_instructions="stub:timeout")) == "timeout"


async def test_stub_malformed_fails_strict_validation() -> None:
    from pydantic import ValidationError

    result = await run_listing_pipeline(_ctx(user_instructions="stub:malformed"), FakeDeps())
    assert result.attempts_used == 7
    with pytest.raises(ValidationError, match="attempts_used"):
        GenerationResult.model_validate_json(result.model_dump_json())


def test_stub_extra_key_is_dropped_by_the_wire_model() -> None:
    """An extra key is not malformed: the seam models are `extra="ignore"` and drop it, so no
    `stub:extra_key` scenario is needed (the result cannot carry the key at all)."""
    data = json.loads((FIXTURES / "GenerationResult.json").read_text(encoding="utf-8"))
    data["unexpected"] = 1
    if isinstance(data.get("listing"), dict):
        data["listing"]["unexpected"] = 1
    result = GenerationResult.model_validate(data)
    assert "unexpected" not in result.model_dump_json()


@pytest.mark.parametrize("text", ["stub:nope", "stub:", "Stub:timeout", " please stub:timeout"])
async def test_stub_unknown_scenario_still_ok(text: str) -> None:
    result = await run_listing_pipeline(_ctx(user_instructions=text), FakeDeps())
    assert result.terminal_state == "generated"
    GenerationResult.model_validate_json(result.model_dump_json())


# ---- SI4 A7 (AC5; CP-D7): `long_title` is auto-fixable, `blocked` stays blocked ----

TRIM_MAX_SHARE = Decimal("0.15")  # a copy of app.services.auto_fix.policy (no `app` import here)


def _trim_share(title: str, limit: int) -> Decimal | None:
    """The share a word-boundary trim to ``limit`` chars removes (A2's rule), else None."""
    cut = title.rfind(" ", 0, limit + 1)
    if cut <= 0:
        return None
    kept = title[:cut].rstrip(" ,;:-/&")
    return Decimal(len(title) - len(kept)) / Decimal(len(title))


def test_stub_lengths() -> None:
    assert (LONG_TITLE_LENGTH, FIXABLE_TITLE_LENGTH) == (260, 212)
    assert "long_title" in SCENARIOS


async def test_stub_scenario_long_title_stays_long_without_a_fix() -> None:
    deps = FakeDeps()
    result = await _run(_ctx(user_instructions="stub:long_title"), deps)
    assert result.listing is not None
    title = result.listing.content.title
    assert len(title) == FIXABLE_TITLE_LENGTH
    assert [n for n, _ in deps.calls].count("validate_rules") == 3  # FakeDeps never fixes it


async def test_stub_scenario_long_title_passes_when_the_deps_fix_it() -> None:
    class FixingDeps(FakeDeps):
        async def validate_rules(self, listing: CanonicalListing) -> ErrorReport:
            self._hit("validate_rules", listing)
            return ErrorReport(status="passed", listing_id=listing.listing_id)

    deps = FixingDeps()
    result = await _run(_ctx(user_instructions="stub:long_title"), deps)
    assert result.terminal_state == "generated"
    assert result.attempts_used == 1
    assert [n for n, _ in deps.calls].count("validate_rules") == 1
    assert result.listing is not None and len(result.listing.content.title) == FIXABLE_TITLE_LENGTH


async def test_stub_blocked_title_is_declined_by_a_trim_and_long_title_accepted() -> None:
    blocked = await _run(_ctx(user_instructions="stub:blocked"))
    fixable = await _run(_ctx(user_instructions="stub:long_title"))
    assert blocked.listing is not None and fixable.listing is not None
    declined = _trim_share(blocked.listing.content.title, TITLE_LIMIT)
    accepted = _trim_share(fixable.listing.content.title, TITLE_LIMIT)
    assert declined is not None and declined > TRIM_MAX_SHARE
    assert accepted is not None and accepted <= TRIM_MAX_SHARE


# ---- SI3 T10: the photo scenarios ----

MAIN = "6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d11"  # the fixture's main image
LABEL = "6f1d2c3b-8a9e-4b70-9c55-2e4f6a8b0d13"  # the fixture's packaging label
PHOTO_TARGETS = [
    {
        "key": "neckline",
        "mode": "fill",
        "allowed_values": ["round neck", "v-neck"],
        "min_confidence": 0.7,
    },
    {"key": "color", "mode": "fill", "allowed_values": ["maroon", "red"], "min_confidence": 0.7},
    {
        "key": "fit",
        "mode": "check",
        "allowed_values": ["regular fit", "slim fit"],
        "min_confidence": 0.7,
    },
    {
        "key": "number_of_pieces",
        "mode": "check",
        "allowed_values": [str(n) for n in range(1, 13)],
        "min_confidence": 0.7,
    },
]


def _photo_ctx(name: str, **over: Any) -> GenerationContext:
    product = {"title": "Kurta", "attributes": {"fit": "Regular Fit"}}
    base: dict[str, Any] = {
        "user_instructions": f"stub:{name}",
        "photo_targets": PHOTO_TARGETS,
        "product": product,
    }
    base.update(over)
    return _ctx(**base)


def _photo_record(result: GenerationResult) -> Any:
    found = [r for r in result.agent_runs if (r.prompt_version or "").startswith("photo_facts/")]
    assert len(found) <= 1
    return found[0] if found else None


def _seen(result: GenerationResult) -> dict[str, tuple[str, float]]:
    return {o.key: (o.value, o.confidence) for o in result.photo_observations}


PHOTO_NAMES = (
    "photo_ok",
    "photo_low",
    "photo_mix",
    "photo_conflict",
    "photo_pieces",
    "photo_hostile",
    "photo_timeout",
    "photo_inject",
)


@pytest.mark.parametrize("name", PHOTO_NAMES)
def test_photo_scenarios_are_known(name: str) -> None:
    assert scenario(_photo_ctx(name)) == name


async def test_photo_ok() -> None:
    deps = FakeDeps()
    result = await _run(_photo_ctx("photo_ok"), deps)
    assert result.terminal_state == "generated"
    assert _seen(result) == {"neckline": ("round neck", 0.95), "color": ("maroon", 0.95)}
    assert {str(o.image_id) for o in result.photo_observations} == {MAIN}
    record = _photo_record(result)
    assert record.agent_name == "image" and record.trigger == "vision"
    assert record.prompt_version == "photo_facts/v1" and record.status == "completed"
    assert str(record.cost_usd) == "0.004000"
    assert [a for n, a in deps.calls if n == "get_model_credentials"].count(("vision",)) == 1


async def test_photo_low_and_mix() -> None:
    low = await _run(_photo_ctx("photo_low"))
    assert _seen(low) == {"neckline": ("round neck", 0.55), "color": ("maroon", 0.55)}
    mix = await _run(_photo_ctx("photo_mix"))
    assert _seen(mix) == {"neckline": ("round neck", 0.95), "color": ("maroon", 0.55)}


async def test_photo_conflict_picks_a_different_value() -> None:
    result = await _run(_photo_ctx("photo_conflict"))
    assert _seen(result) == {"fit": ("slim fit", 0.95)}


async def test_photo_pieces() -> None:
    result = await _run(_photo_ctx("photo_pieces"))
    assert _seen(result) == {"number_of_pieces": ("3", 0.95)}


async def test_photo_hostile_validates_with_sixteen_items() -> None:
    result = await _run(_photo_ctx("photo_hostile"))
    assert len(result.photo_observations) == 16
    again = GenerationResult.model_validate_json(result.model_dump_json())
    keys = {o.key for o in again.photo_observations}
    assert {"mrp", "fiber_composition"} <= keys
    assert LABEL in {str(o.image_id) for o in again.photo_observations}


async def test_photo_timeout_has_a_failed_record_and_no_observations() -> None:
    result = await _run(_photo_ctx("photo_timeout"))
    record = _photo_record(result)
    assert record.status == "failed" and record.error_report.error_code is ErrorCode.TIMEOUT
    assert result.photo_observations == [] and result.terminal_state == "generated"


async def test_photo_inject() -> None:
    result = await _run(_photo_ctx("photo_inject"))
    assert result.listing is not None and "handmade" in result.listing.content.description.lower()
    assert result.listing.attributes["construction"] == "handmade"
    assert _seen(result) == {"construction": ("handmade", 0.95)}


@pytest.mark.parametrize("name", PHOTO_NAMES)
async def test_photo_scenarios_without_targets_are_ok(name: str) -> None:
    deps = FakeDeps()
    result = await _run(_photo_ctx(name, photo_targets=[]), deps)
    assert result.photo_observations == [] and _photo_record(result) is None
    assert ("vision",) not in [a for n, a in deps.calls if n == "get_model_credentials"]


async def test_ok_with_targets_returns_no_observations() -> None:
    result = await _run(_photo_ctx("ok"))
    assert result.photo_observations == [] and _photo_record(result) is None


@pytest.mark.parametrize("name", PHOTO_NAMES)
async def test_photo_scenarios_byte_identical(name: str) -> None:
    ctx = _photo_ctx(name)
    assert (await _run(ctx)).model_dump_json() == (await _run(ctx)).model_dump_json()


async def test_photo_record_id_is_stable() -> None:
    from uuid import uuid5

    ctx = _photo_ctx("photo_ok")
    assert _photo_record(await _run(ctx)).id == uuid5(ctx.job_id, "photo:1")


async def test_photo_observations_cap_holds_in_the_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    from mkos_pipeline import graph
    from pydantic import ValidationError

    real = graph._observations

    def seventeen(ctx: GenerationContext, chosen: str, image_id: UUID) -> list[Any]:
        items = real(ctx, "photo_hostile", image_id)
        return [*items, items[0]]

    monkeypatch.setattr(graph, "_observations", seventeen)
    with pytest.raises(ValidationError):
        await _run(_photo_ctx("photo_ok"))
