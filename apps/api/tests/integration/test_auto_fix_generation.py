"""SI4 A9, A10 (AC5-AC8): the generation job fixes the pipeline's final listing with the same
pure pass the correction loop ran, stores the fixed listing with its trail, and never fixes a
human edit."""

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
    """AC7: the stored listing equals `auto_fix` re-run on the pipeline's raw final listing,
    after the seller-value overlay the worker applies first (SI2 T16)."""
    from app.repositories.uow import tenant_uow
    from app.services.auto_fix.apply import auto_fix
    from app.services.auto_fix.fixers import FixContext
    from app.services.generation_result import recheck
    from app.services.rules_loader import load_rules
    from app.services.seller_facts import seller_overlay
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
    raw = seller_overlay(raw, facts, rules.template)
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


# ---- A10 (AC6): the trail ----


def audits(world: GenWorld, listing_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT actor_type, actor_user_id, after, reason FROM audit_log "
        "WHERE entity_id = %s AND action = 'listing.auto_fixed' ORDER BY occurred_at",
        [listing_id],
    ).fetchall()


async def test_the_trail_row_and_audit(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world, scenario="stub:long_title")
    fixed = [r for r in issues(gen_world, made.listing_id) if r["status"] == "auto_fixed"]
    assert len(fixed) == 1
    row = fixed[0]
    assert (row["resolution"], row["fix_action"], row["auto_fixable"]) == (
        "deterministic_fix",
        "reformat",
        True,
    )
    assert (row["field_path"], row["error_code"], row["source"]) == (
        "content.title",
        "FIELD_TOO_LONG",
        "rules_engine",
    )
    assert row["action_taken"] and "title" in row["action_taken"]
    assert STUB_LONG_TITLE not in row["action_taken"]
    assert row["listing_version_id"] == made.version_id
    assert row["resolved_by_user_id"] is None
    [(actor, user, after, reason)] = audits(gen_world, made.listing_id)
    assert (actor, user) == ("system", None)
    assert after == {"status": "validated"}
    tag = reason[reason.index("[") :]
    assert tag.startswith("[reformat content.title v")
    assert len(tag.split()[-1].split("\u2192")[0]) == 16
    assert STUB_LONG_TITLE[:40] not in reason
    stored = content(gen_world, made.version_id)["content"]["content"]["title"]
    assert stored not in reason

    headers = bearer_for(gen_world, OPERATOR)
    listed = await api.get(
        "/api/v1/issues",
        params={"status": "auto_fixed", "listing_id": str(made.listing_id)},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    [shown] = listed.json()["data"]
    assert shown["id"] == str(row["id"])
    assert shown["action_taken"] == row["action_taken"]
    history = await api.get(f"/api/v1/listings/{made.listing_id}/history", headers=headers)
    assert history.status_code == 200, history.text
    assert "listing.auto_fixed" in [h["action"] for h in history.json()["data"]]


async def test_a_replayed_persist_writes_no_second_trail(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.uow import tenant_uow
    from app.services.generation_persist import make_persist as real
    from app.workers import generation

    plans: list[Any] = []

    def keep(plan: Any) -> Any:
        plans.append(plan)
        return real(plan)

    monkeypatch.setattr(generation, "make_persist", keep)
    made = await generated(gen_world, scenario="stub:long_title")
    before = (len(issues(gen_world, made.listing_id)), len(audits(gen_world, made.listing_id)))
    async with tenant_uow(gen_world.tenant("acme")) as uow:
        await real(plans[-1])(uow)
    after = (len(issues(gen_world, made.listing_id)), len(audits(gen_world, made.listing_id)))
    assert before == after == (1, 1)


async def test_a_fix_of_an_older_open_row_shows_in_the_fixing_version(gen_world: GenWorld) -> None:
    """CP-D2 and the version review: the row an older version left open closes the way the
    sync closes it for the cause (`resolved`/`regenerated`, on its own version), and the fix is
    a fresh `auto_fixed` row on the version the fix pass wrote, linked back by
    `recurrence_of_id` (the guard keeps `listing_version_id` immutable). No row reopens."""
    from app.repositories.uow import tenant_uow
    from app.services.generation import request_regeneration

    from .review_support import run_job

    first = await generated(gen_world, scenario="stub:blocked")
    [open_row] = [r for r in issues(gen_world, first.listing_id) if r["status"] == "open"]
    assert open_row["error_code"] == "FIELD_TOO_LONG"
    principal = gen_world.principal(OPERATOR)
    async with tenant_uow(principal.tenant_id, user_id=principal.user_id) as uow:
        accepted = await request_regeneration(
            uow, principal, str(uuid.uuid4()), first.listing_id, "stub:long_title"
        )
    await run_job(accepted.job_id)
    listing = gen_world.listing(first.listing_id)
    assert listing["status"] == "validated"
    fixing_version = listing["current_version_id"]
    assert fixing_version != first.version_id
    rows = issues(gen_world, first.listing_id)
    assert [
        (r["id"], r["status"], r["resolution"], r["listing_version_id"], r["occurrences"])
        for r in rows
    ] == [
        (open_row["id"], "resolved", "regenerated", first.version_id, 1),
        (rows[1]["id"], "auto_fixed", "deterministic_fix", fixing_version, 1),
    ]
    assert rows[1]["recurrence_of_id"] == open_row["id"]
    assert rows[1]["action_taken"] and rows[1]["dedupe_key"] == open_row["dedupe_key"]
    assert [r for r in rows if r["status"] == "open"] == []
