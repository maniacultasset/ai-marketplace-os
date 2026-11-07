"""SI4 A9 (AC5, AC7, AC8): the generation job fixes the pipeline's final listing with the same
pure pass the correction loop ran, stores the fixed listing, and never fixes a human edit."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .products_support import bearer_for
from .review_support import OPERATOR, Made, generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
UNIT = "Cotton Kurta Set for Women with Straight Fit and Round Neck "
STUB_LONG_TITLE = (UNIT * 5)[:211] + "."  # the stub's `long_title` (A7)


def content(world: GenWorld, version_id: uuid.UUID) -> dict[str, Any]:
    row = world.conn.execute(
        "SELECT content_json, attempts_used FROM listing_versions WHERE id = %s", [version_id]
    ).fetchone()
    assert row is not None
    return {"content": row[0], "attempts_used": row[1]}


def issues(world: GenWorld, listing_id: uuid.UUID) -> list[dict[str, Any]]:
    cur = world.conn.execute(
        "SELECT * FROM listing_issues WHERE listing_id = %s ORDER BY created_at, id", [listing_id]
    )
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def with_attribute(monkeypatch: pytest.MonkeyPatch, key: str, value: Any) -> None:
    """The stub pipeline, with one attribute of its final listing set to ``value``."""
    from app.workers import generation
    from mkos_pipeline import run_listing_pipeline as real

    async def patched(ctx: Any, deps: Any) -> Any:
        result = await real(ctx, deps)
        listing = result.listing
        assert listing is not None
        attributes = {**listing.attributes}
        if value is None:
            attributes.pop(key, None)
        else:
            attributes[key] = value
        return result.model_copy(
            update={"listing": listing.model_copy(update={"attributes": attributes})}
        )

    monkeypatch.setattr(generation, "run_listing_pipeline", patched)


async def test_long_title_is_fixed_and_validated(gen_world: GenWorld) -> None:
    """AC5: one rules call, the stored title within 200 and a prefix of the stub's title."""
    made = await generated(gen_world, scenario="stub:long_title")
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("validated", None)
    stored = content(gen_world, made.version_id)
    title = stored["content"]["content"]["title"]
    assert stored["attempts_used"] == 1
    assert len(title) <= 200
    assert STUB_LONG_TITLE.startswith(title)


async def test_blocked_stays_blocked(gen_world: GenWorld) -> None:
    made = await generated(gen_world, scenario="stub:blocked")
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "corrections_exhausted")
    assert content(gen_world, made.version_id)["attempts_used"] == 3


async def test_ok_has_no_fix(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    title = content(gen_world, made.version_id)["content"]["content"]["title"]
    assert title == "Cotton Kurta Set for Women with Straight Fit and Round Neck."
    assert [i for i in issues(gen_world, made.listing_id) if i["status"] == "auto_fixed"] == []


async def test_an_enum_spelling_is_remapped(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_attribute(monkeypatch, "neckline", "round-neck")
    made = await generated(gen_world)
    assert gen_world.listing(made.listing_id)["status"] == "validated"
    stored = content(gen_world, made.version_id)["content"]
    assert stored["attributes"]["neckline"] == "Round Neck"


async def test_the_stored_listing_is_the_pure_pass_on_the_raw_output(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: the stored listing equals `auto_fix` re-run on the pipeline's raw final listing."""
    from app.repositories.uow import tenant_uow
    from app.services.auto_fix.apply import auto_fix
    from app.services.auto_fix.fixers import FixContext
    from app.services.generation_result import recheck
    from app.services.rules_loader import load_rules
    from app.workers import generation
    from mkos_pipeline import run_listing_pipeline as real

    seen: list[Any] = []

    async def spy(ctx: Any, deps: Any) -> Any:
        result = await real(ctx, deps)
        seen.append(result.listing)
        return result

    monkeypatch.setattr(generation, "run_listing_pipeline", spy)
    made = await generated(gen_world, scenario="stub:long_title")
    stored = content(gen_world, made.version_id)["content"]
    tenant = gen_world.tenant("acme")
    async with tenant_uow(tenant) as uow:
        listing = await uow.listings.get(made.listing_id)
        assert listing is not None
        rules = await load_rules(
            uow, tenant_id=tenant, marketplace=listing.marketplace, category=listing.category
        )
        critical = await uow.product_facts.critical_keys()
        facts = await uow.product_facts.resolve(listing.product_id)
    from app.core.resolved_facts import for_rules

    assert facts is not None
    product = for_rules(facts)
    raw = seen[0].model_copy(update={"images": [], "listing_id": listing.id})
    ctx = FixContext(ruleset=rules.ruleset, template=rules.template, critical_keys=critical)
    again = auto_fix(raw, lambda item: recheck(item, rules, product), ctx)
    assert again.listing.content.model_dump(mode="json") == stored["content"]
    assert again.listing.attributes == stored["attributes"]
    assert len(again.records) == 1


async def test_patch_never_auto_fixed(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """AC8 (D6): a human edit with a fixable title is checked, never fixed."""
    made: Made = await generated(gen_world)
    headers = {**bearer_for(gen_world, OPERATOR), "If-Match": f'"{made.version_id}"'}
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={
            "content": {
                "title": STUB_LONG_TITLE,
                "bullets": ["Soft cotton fabric"],
                "description": "A cotton kurta set with a straight fit.",
            }
        },
    )
    assert response.status_code == 200, response.text
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["status_reason"]) == ("blocked", "validation_errors")
    version = uuid.UUID(response.json()["current_version_id"])
    assert content(gen_world, version)["content"]["content"]["title"] == STUB_LONG_TITLE
    rows = issues(gen_world, made.listing_id)
    too_long = [r for r in rows if r["error_code"] == "FIELD_TOO_LONG"]
    assert [r["status"] for r in too_long] == ["open"]
    assert [r for r in rows if r["status"] == "auto_fixed"] == []
