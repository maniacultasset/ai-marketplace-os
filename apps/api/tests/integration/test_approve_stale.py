"""S19 T10 (DW2; AC 3; D3; DATABASE.md:823 G9): an approve names the current version, and that
version needs a passed `rules_engine` run with the currently resolved rules version, template
and facts digest."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import httpx
import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import (
    REVIEWER,
    decisions,
    edited,
    generated,
    item,
    only_item,
    post_approve,
)

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
STALE = (409, "STALE_VERSION")


def _code(result: dict[str, Any]) -> tuple[int, str | None]:
    error = result["error"]
    return result["http_status"], None if error is None else error["error_code"]


async def test_approve_stale_version(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """An older, a random and another tenant's version id answer the same item body."""
    first = await generated(gen_world)
    current = await edited(api, gen_world, first)
    bharat = await generated(gen_world, "operator@bharat.test")
    bodies = []
    for version in (first.version_id, uuid.uuid4(), bharat.version_id):
        body = {**item(current), "listing_version_id": str(version)}
        result = only_item(await post_approve(api, gen_world, REVIEWER, [body]))
        assert _code(result) == STALE
        bodies.append(result["error"]["errors"])
    assert bodies[0] == bodies[1] == bodies[2]
    assert decisions(gen_world, current.listing_id) == 0


def _bump_rules(world: GenWorld) -> None:
    """A rule revised after the run: the resolved rules version moves (admin login)."""
    world.conn.execute(
        "UPDATE rules SET revision = nextval('rules_revision_seq') WHERE id = (SELECT id FROM "
        "rules WHERE marketplace = 'amazon' AND tenant_id IS NULL ORDER BY id LIMIT 1)"
    )


def _shift(monkeypatch: pytest.MonkeyPatch, **change: Any) -> None:
    """The currently resolved pins differ from the stored run's in one field."""
    import app.services.review as review

    original = review.current_pins  # type: ignore[attr-defined]

    async def shifted(uow: Any, listing: Any) -> Any:
        return dataclasses.replace(await original(uow, listing), **change)

    monkeypatch.setattr(review, "current_pins", shifted)


async def test_approve_stale_run_rules_revision(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """A rule revised after the run raises the resolved rules version."""
    made = await generated(gen_world)
    _bump_rules(gen_world)
    result = only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))
    assert _code(result) == STALE
    assert gen_world.listing(made.listing_id)["status"] == "validated"


@pytest.mark.parametrize(
    "change",
    [{"template_version": 99}, {"template_id": uuid.uuid4()}, {"facts_digest": b"\x02" * 32}],
    ids=["promoted_template_version", "promoted_template", "facts_digest"],
)
async def test_approve_stale_run_pins(
    api: httpx.AsyncClient,
    gen_world: GenWorld,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    made = await generated(gen_world)
    _shift(monkeypatch, **change)
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))) == STALE


async def test_approve_stale_run_no_passed_run(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    """A newer failed run with the same pins decides (the newest equal-pins run)."""
    made = await generated(gen_world)
    gen_world.conn.execute(
        "INSERT INTO validation_runs (tenant_id, listing_id, listing_version_id, marketplace, "
        "source, passed, error_report_json, attempt, rules_version, category_template_id, "
        "template_version, facts_digest, blocking_count, warning_count, created_at) "
        "SELECT tenant_id, listing_id, listing_version_id, marketplace, source, false, "
        "error_report_json, attempt, rules_version, category_template_id, template_version, "
        "facts_digest, 1, 0, created_at + interval '1 second' FROM validation_runs "
        "WHERE listing_version_id = %s AND source = 'rules_engine' "
        "ORDER BY created_at DESC LIMIT 1",
        [made.version_id],
    )
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))) == STALE


async def test_approve_after_validate_refresh_ok(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    """After a rule change, S18's `validate` writes a run with the fresh pins; approve passes."""
    from .products_support import bearer_for

    made = await generated(gen_world)
    _bump_rules(gen_world)
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))) == STALE
    response = await api.post(
        "/api/v1/listings/validate",
        headers=bearer_for(gen_world, "operator@acme.test"),
        json={"listing_id": str(made.listing_id)},
    )
    assert response.status_code == 200 and response.json()["passed"] is True
    assert _code(only_item(await post_approve(api, gen_world, REVIEWER, [item(made)]))) == (
        200,
        None,
    )
