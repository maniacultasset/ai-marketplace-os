import pytest
from mkos_schemas.jobs import GenerationResult
from tests.fakes import FakeDeps, ProductAwareTransport, make_context

from mkos_pipeline.graph import run_listing_pipeline
from mkos_pipeline.llm.client import reset_transport, set_transport

EXPECTED_STAGES = [
    "load_context",
    "retrieve_brand",
    "content_agent",
    "keyword_agent",
    "attribute_agent",
    "image_agent",
    "merge_outputs",
    "validate_schema",
    "validate_rules",
    "validate_vision",
    "correct",
    "quality_check",
]


def _started(events: list[tuple[str, str]]) -> list[str]:
    return [node for node, status in events if status == "started"]


@pytest.mark.asyncio
async def test_stage_order_and_pass() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)
    token = set_transport(ProductAwareTransport())
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    GenerationResult.model_validate(result.model_dump())
    assert _started(deps.events) == EXPECTED_STAGES
    assert result.terminal_state == "generated"
    assert result.listing is not None
    assert result.listing.product_id == ctx.product_id
    assert result.listing.marketplace == ctx.marketplace
    assert result.listing.category == ctx.category
    assert result.listing.locale == "en-IN"
    assert result.attempts_used == 1
    assert deps.rule_calls == 1
    assert all(block.status in {"ok", "skipped"} for block in result.blocks)


@pytest.mark.asyncio
async def test_rules_fail_then_pass_on_attempt_two() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)
    calls = {"n": 0}

    class Flaky(ProductAwareTransport):
        def __init__(self) -> None:
            super().__init__()
            self.content_generates = 0

        async def __call__(self, payload, timeout):
            calls["n"] += 1
            user = payload["messages"][1]["content"].lower()
            if "listing copy" in user:
                self.content_generates += 1
                self.long_title = self.content_generates == 1
            else:
                self.long_title = False
            return await super().__call__(payload, timeout)

    token = set_transport(Flaky())
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    assert result.terminal_state == "generated"
    assert result.attempts_used == 2
    assert deps.rule_calls == 2


@pytest.mark.asyncio
async def test_long_title_blocks_after_exactly_three_validate_rules_calls() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)
    token = set_transport(ProductAwareTransport(long_title=True))
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    assert result.terminal_state == "blocked"
    assert result.attempts_used == 3
    assert deps.rule_calls == 3
    assert result.listing is not None
    assert len(result.listing.content.title) == 221


@pytest.mark.asyncio
async def test_attempts_used_never_exceeds_three() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)
    token = set_transport(ProductAwareTransport(long_title=True))
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    assert result.attempts_used <= 3
    assert deps.rule_calls <= 3


@pytest.mark.asyncio
async def test_provider_error_still_returns_result() -> None:
    ctx = make_context()
    deps = FakeDeps(ctx)

    async def boom(_payload, _timeout):
        raise RuntimeError("provider down")

    token = set_transport(boom)
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    GenerationResult.model_validate(result.model_dump())
    assert result.terminal_state == "failed"
    assert result.listing is None
    assert any(record.status == "failed" for record in result.agent_runs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "marketplace"),
    [
        ("ethnic_wear_kurta_sets", "amazon"),
        ("ethnic_wear_kurta_sets", "flipkart"),
        ("tshirts", "amazon"),
        ("tshirts", "flipkart"),
        ("home_decor_lamps", "amazon"),
        ("home_decor_lamps", "flipkart"),
    ],
)
async def test_listing_returned_for_launch_categories(category, marketplace) -> None:
    product = {
        "sku": "X-001",
        "identity": {"title": "Sample Product", "brand_name": "Acme"},
        "attributes": {"color": "blue", "fabric_type": "cotton", "product_dimensions": "20x10x10"},
    }
    ctx = make_context(category=category, marketplace=marketplace, product=product)
    deps = FakeDeps(ctx)
    token = set_transport(ProductAwareTransport())
    try:
        result = await run_listing_pipeline(ctx, deps)
    finally:
        reset_transport(token)
    assert result.listing is not None
    assert result.listing.category == category
    assert result.listing.marketplace == marketplace
