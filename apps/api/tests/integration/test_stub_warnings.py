"""S19 T22 (AC 13, 16; CP-D5): acme's seeded warning rule and the stub's `warnings` scenario. The
stub cannot fabricate a warning: only the backend's engine re-check with the tenant rule makes the
listing `needs_review` with one open warning issue."""

from __future__ import annotations

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import generated

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


def _rules(world: GenWorld, slug: str) -> list[tuple[object, ...]]:
    return world.conn.execute(
        "SELECT field, rule_type, severity, rule_def->'terms', enabled FROM rules "
        "WHERE tenant_id = %s AND rule_def->'terms' ? 'stubwarn'",
        [world.tenant(slug)],
    ).fetchall()


async def test_seed_acme_warning_rule_idempotent(gen_world: GenWorld) -> None:
    from app.cli import run_seed
    from app.db.session import dispose_engine

    assert _rules(gen_world, "acme") == [
        ("content.title", "banned_terms", "warning", ["stubwarn"], True)
    ]
    assert await run_seed("demo", reset=False) == 0
    await dispose_engine()
    assert len(_rules(gen_world, "acme")) == 1


async def test_seed_bharat_has_no_warning_rule(gen_world: GenWorld) -> None:
    assert _rules(gen_world, "bharat") == []


async def test_stub_warnings_needs_engine_rule(gen_world: GenWorld) -> None:
    with_rule = await generated(gen_world, scenario="stub:warnings")
    assert gen_world.listing(with_rule.listing_id)["status"] == "needs_review"
    issues = gen_world.conn.execute(
        "SELECT severity, field_path, error_code, status FROM listing_issues WHERE listing_id = %s",
        [with_rule.listing_id],
    ).fetchall()
    assert issues == [("warning", "content.title", "VALUE_NOT_ALLOWED", "open")]
    gen_world.conn.execute("UPDATE rules SET enabled = false WHERE rule_def->'terms' ? 'stubwarn'")
    without = await generated(gen_world, scenario="stub:warnings")
    assert gen_world.listing(without.listing_id)["status"] == "validated"
    assert gen_world.count("listing_issues", "listing_id = %s", [without.listing_id]) == 0
