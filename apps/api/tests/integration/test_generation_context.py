"""S18 T11 (DW4, AC 8; D3-D6; CANON.md:118, :125): the `GenerationContext` built at claim, from the
demo seed as `app_rw`."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating, set_status
from .products_support import statements

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
SECRETS = {
    "OPENAI_API_KEY": "sk-sentinel-openai-0001",
    "DEEPSEEK_API_KEY": "sentinel-deepseek-0002",
    "AMAZON_CLIENT_SECRET": "sentinel-amazon-secret-0003",
    "FLIPKART_CLIENT_SECRET": "sentinel-flipkart-secret-0004",
    "SENTRY_DSN": "https://sentinel0005@o1.ingest.sentry.io/1",
}


async def _context(world: GenWorld, job_id: uuid.UUID, *, max_attempts: int = 3) -> Any:
    from app.repositories.uow import tenant_uow
    from app.services.generation_context import build_context
    from app.services.rules_loader import load_rules

    acme = world.tenant("acme")
    async with tenant_uow(acme) as uow:
        job = await uow.job_runs.get_by_id(job_id)
        assert job is not None and job.listing_id is not None
        listing = await uow.listings.get(job.listing_id)
        assert listing is not None
        rules = await load_rules(
            uow, tenant_id=acme, marketplace=listing.marketplace, category=listing.category
        )
        facts = await uow.product_facts.resolve(listing.product_id)
        assert facts is not None
        return await build_context(
            uow,
            job,
            listing,
            rules=rules,
            facts=facts,
            request_id="req-s18-ctx",
            max_attempts=max_attempts,
        )


async def test_context_contents(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world, instructions="Lead with silk.")
    ctx = await _context(gen_world, accepted.job_id)
    assert (ctx.job_id, ctx.listing_id, ctx.tenant_id) == (
        accepted.job_id,
        accepted.listing_id,
        gen_world.tenant("acme"),
    )
    assert ctx.product["sku"] == "KURTA-002"
    assert ctx.product["identity"]["title"] == "Silk Kurta Set"
    assert ctx.brand_profile["guidelines"] == [
        "Lead with fabric and fit.",
        "Never claim 'best' or 'guaranteed'.",
    ]
    assert "brand_description" in ctx.brand_profile
    assert ctx.marketplace_rules.marketplace.value == "amazon"
    assert all(
        s.marketplace_field is None and not s.value_map
        for s in ctx.category_template.specs.values()
    )
    assert ctx.user_instructions == "Lead with silk."
    assert (ctx.locale, ctx.max_attempts, ctx.previous_version) == ("en-IN", 3, None)


async def test_context_has_no_credentials(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    acme = gen_world.tenant("acme")
    f.marketplace_account(
        gen_world.conn,
        acme,
        token_enc=b"sentinel-token-0006",
        settings=Jsonb({"refresh_token": "sentinel-refresh-0007"}),
    )
    party = f.party(
        gen_world.conn,
        acme,
        legal_name="Silk House Pvt Ltd",
        email="sentinel-0008@silk.example",
        phone="+91-0009-0009",
        gstin="29SENTI1234F1Z9",
    )
    product = gen_world.product("acme", "KURTA-002")
    gen_world.conn.execute(
        "UPDATE products SET manufacturer_party_id = %s, "
        "provenance = provenance || %s::jsonb WHERE id = %s",
        [party, json.dumps({"manufacturer": f.fact(value=None)}), product],
    )
    accepted = await make_generating(gen_world)
    shown = (await _context(gen_world, accepted.job_id)).model_dump_json()
    for sentinel in [
        *SECRETS.values(),
        "sentinel-token-0006",
        "sentinel-refresh-0007",
        "sentinel-0008@silk.example",
        "+91-0009-0009",
        "29SENTI1234F1Z9",
        get_settings().jwt_secret or "never",
    ]:
        assert sentinel not in shown
    assert "Silk House Pvt Ltd" in shown  # the legal name is kept
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys.update(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(shown))
    assert not keys & {"token", "token_enc", "account", "marketplace_account_id", "gstin"}


async def test_context_regenerate_instructions_from_decision(gen_world: GenWorld) -> None:
    from app.db.seed_ops import demo_listing_ids
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    listing = demo_listing_ids()[0]
    set_status(gen_world, listing, "needs_review")
    principal = gen_world.principal("reviewer@acme.test")
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        accepted = await request_regeneration(
            uow, principal, str(uuid.uuid4()), listing, "Use the word handloom."
        )
    gen_world.conn.execute(
        "UPDATE job_runs SET payload_json = %s WHERE id = %s",
        [Jsonb({"user_instructions": "payload text"}), accepted.job_id],
    )
    ctx = await _context(gen_world, accepted.job_id)
    assert ctx.user_instructions == "Use the word handloom."


async def test_context_generate_instructions_from_payload(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world, instructions="From the payload.")
    assert (await _context(gen_world, accepted.job_id)).user_instructions == "From the payload."


async def test_context_review_feedback_max_5(gen_world: GenWorld) -> None:
    from app.db.seed_ops import demo_listing_ids

    listing, version, _ = demo_listing_ids()
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for n in range(7):
        f.review_decision(
            gen_world.conn,
            gen_world.tenant("acme"),
            listing,
            version,
            decision="reject",
            reason_code="other",
            feedback=f"too long {n}",
            decided_at=base + timedelta(minutes=n),
        )
    accepted = await make_generating(gen_world, sku="KURTA-001")
    ctx = await _context(gen_world, accepted.job_id)
    assert ctx.review_feedback == [f"too long {n}" for n in (6, 5, 4, 3, 2)]


async def test_context_previous_version_current(gen_world: GenWorld) -> None:
    from app.db.seed_ops import demo_listing_content, demo_listing_ids

    accepted = await make_generating(gen_world, sku="KURTA-001")
    ctx = await _context(gen_world, accepted.job_id)
    assert ctx.previous_version is not None
    assert ctx.previous_version.listing_id == demo_listing_ids()[0]
    assert ctx.previous_version.content.title == demo_listing_content()["title"]


async def test_context_template_gone_category_not_mapped(gen_world: GenWorld) -> None:
    from app.services.generation_context import GenerationContextError
    from mkos_schemas.codes import ErrorCode

    accepted = await make_generating(gen_world)
    gen_world.conn.execute(
        "UPDATE category_templates SET is_current = false WHERE marketplace = 'amazon' "
        "AND category = 'ethnic_wear_kurta_sets'"
    )
    with pytest.raises(GenerationContextError) as caught:
        await _context(gen_world, accepted.job_id)
    assert caught.value.code is ErrorCode.CATEGORY_NOT_MAPPED


async def test_context_eight_statements(gen_world: GenWorld) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation_context import build_context
    from app.services.rules_loader import load_rules

    accepted = await make_generating(gen_world, sku="KURTA-001")
    acme = gen_world.tenant("acme")
    async with tenant_uow(acme) as uow:
        job = await uow.job_runs.get_by_id(accepted.job_id)
        listing = await uow.listings.get(accepted.listing_id)
        assert job is not None and listing is not None
        rules = await load_rules(
            uow, tenant_id=acme, marketplace=listing.marketplace, category=listing.category
        )
        facts = await uow.product_facts.resolve(listing.product_id)
        assert facts is not None
        with statements() as seen:
            await build_context(
                uow, job, listing, rules=rules, facts=facts, request_id="r", max_attempts=3
            )
    reads = [s for s in seen if s.lstrip().startswith("SELECT") and "set_config" not in s]
    assert len(reads) <= 8, reads


async def test_context_unicode_instructions(gen_world: GenWorld) -> None:
    text = "शीर्षक छोटा रखें 🙏 (keep it short)"
    accepted = await make_generating(gen_world, instructions=text)
    assert (await _context(gen_world, accepted.job_id)).user_instructions == text


async def test_context_secret_walk_refuses_secret_shaped_instruction(
    gen_world: GenWorld, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.generation_context import GenerationContextError
    from mkos_schemas.codes import ErrorCode

    accepted = await make_generating(gen_world)
    # the API refuses this text (CP12), so it is written straight into the payload
    gen_world.conn.execute(
        "UPDATE job_runs SET payload_json = %s WHERE id = %s",
        [Jsonb({"user_instructions": 'please use {"api_key": "x-sentinel-ctx"}'}), accepted.job_id],
    )
    with pytest.raises(GenerationContextError) as caught:
        await _context(gen_world, accepted.job_id)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert "x-sentinel-ctx" not in str(caught.value) and "x-sentinel-ctx" not in caplog.text


async def test_context_max_attempts_clamped(gen_world: GenWorld) -> None:
    accepted = await make_generating(gen_world)
    assert (await _context(gen_world, accepted.job_id, max_attempts=9)).max_attempts == 3
    assert (await _context(gen_world, accepted.job_id, max_attempts=1)).max_attempts == 1
