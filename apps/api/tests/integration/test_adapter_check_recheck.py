"""SI9 T8 (AC8, AC9; CP-D1, CP-D3, CP-D4): the marketplace check before approval in the
generation persist, `validate` and the content PATCH, synced as `schema` issues, with SI4's
questions behind them."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from app.adapters.amazon import schema_check
from tests.support.fake_marketplace import ptd_fixture

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, set_status
from .ptd_support import store
from .review_support import OPERATOR, Made, generated, open_issue

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]
VALIDATE = "/api/v1/listings/validate"
# a required attribute no canonical path feeds: the finding lands on `listing`, no question
UNFED: dict[str, Any] = {
    "type": "object",
    "required": ["unfed_x"],
    "properties": {"unfed_x": {"type": "array"}},
}
PERMISSIVE: dict[str, Any] = {"type": "object"}


@pytest.fixture(autouse=True)
def _cold_cache() -> Iterator[None]:
    schema_check.clear_cache()
    yield
    schema_check.clear_cache()


def _kurta() -> dict[str, Any]:
    """The KURTA fixture requires `fit_type` (fed by `attributes.fit`), which the stub's
    listing does not carry."""
    loaded: dict[str, Any] = json.loads(ptd_fixture("KURTA.schema.json"))
    return loaded


def _issues(world: GenWorld, listing_id: uuid.UUID, source: str = "schema") -> list[Any]:
    return world.conn.execute(
        "SELECT field_path, error_code, severity, status, resolution, human_task_id, occurrences "
        "FROM listing_issues WHERE listing_id = %s AND source = %s ORDER BY field_path",
        [listing_id, source],
    ).fetchall()


def _tasks(world: GenWorld, listing_id: uuid.UUID) -> list[Any]:
    return world.conn.execute(
        "SELECT DISTINCT t.id, t.kind, t.status, t.cancelled_reason FROM human_tasks t "
        "JOIN listing_issues i ON i.human_task_id = t.id WHERE i.listing_id = %s",
        [listing_id],
    ).fetchall()


def _runs(world: GenWorld, listing_id: uuid.UUID) -> int:
    return world.count("validation_runs", "listing_id = %s", [listing_id])


def _status(world: GenWorld, listing_id: uuid.UUID) -> tuple[str, str | None]:
    row = world.listing(listing_id)
    return row["status"], row["status_reason"]


async def _validate(api: httpx.AsyncClient, world: GenWorld, made: Made, **body: Any) -> Any:
    response = await api.post(
        VALIDATE,
        headers=world.headers(OPERATOR),
        json={"listing_id": str(made.listing_id), **body},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _patch_fit(api: httpx.AsyncClient, world: GenWorld, made: Made) -> Any:
    row = world.listing(made.listing_id)
    content = world.scalar(
        "SELECT content_json FROM listing_versions WHERE id = %s", [row["current_version_id"]]
    )
    headers = world.headers(OPERATOR)
    headers.pop("Idempotency-Key")
    headers["If-Match"] = f'"{row["current_version_id"]}"'
    response = await api.patch(
        f"/api/v1/listings/{made.listing_id}/content",
        headers=headers,
        json={
            "content": content["content"],
            "attributes": {**content["attributes"], "fit": "Regular"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---- the generation persist (AC8, CP-D1) ----


async def test_generation_blocked_with_a_question(gen_world: GenWorld) -> None:
    store(gen_world.conn, _kurta())
    made = await generated(gen_world)
    assert _status(gen_world, made.listing_id) == ("blocked", "human_task_open")
    issues = _issues(gen_world, made.listing_id)
    fit = [i for i in issues if i[0] == "attributes.fit"]
    assert len(fit) == 1 and fit[0][1:4] == ("MISSING_ATTRIBUTE", "error", "open")
    (task,) = _tasks(gen_world, made.listing_id)
    assert task[1:3] == ("missing_fact", "open") and fit[0][5] == task[0]
    stamp = gen_world.scalar(
        "SELECT content_json->>'status' FROM listing_versions WHERE id = %s", [made.version_id]
    )
    assert stamp == "blocked"


async def test_generation_without_a_target_is_corrections_exhausted(
    gen_world: GenWorld,
) -> None:
    store(gen_world.conn, UNFED)
    made = await generated(gen_world)
    assert _status(gen_world, made.listing_id) == ("blocked", "corrections_exhausted")
    ((field, code, severity, status, _res, task, _n),) = _issues(gen_world, made.listing_id)
    assert (field, code, severity, status, task) == (
        "listing",
        "MISSING_ATTRIBUTE",
        "error",
        "open",
        None,
    )
    assert _tasks(gen_world, made.listing_id) == []


async def test_no_definition_mapping_only(gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    assert _status(gen_world, made.listing_id) == ("validated", None)
    assert [i for i in _issues(gen_world, made.listing_id) if i[2] == "error"] == []


async def test_warning_only_check_keeps_validated(gen_world: GenWorld) -> None:
    store(gen_world.conn, _kurta(), enforced="NOT_ENFORCED")
    made = await generated(gen_world)
    assert _status(gen_world, made.listing_id) == ("validated", None)
    issues = _issues(gen_world, made.listing_id)
    assert issues and {i[2] for i in issues} == {"warning"}


# ---- validate with equal pins (CP-D4, AC9) ----


async def test_validate_after_a_definition_blocks_without_a_new_run(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    runs = _runs(gen_world, made.listing_id)
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made)
    assert _status(gen_world, made.listing_id) == ("blocked", "validation_errors")
    assert gen_world.listing(made.listing_id)["last_event"] == "revalidate"
    assert [i[:4] for i in _issues(gen_world, made.listing_id)] == [
        ("listing", "MISSING_ATTRIBUTE", "error", "open")
    ]
    assert _runs(gen_world, made.listing_id) == runs  # no second validation run


async def test_validate_opens_the_question_then_a_fix_closes_it(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    runs = _runs(gen_world, made.listing_id)
    store(gen_world.conn, _kurta())
    await _validate(api, gen_world, made)
    assert _status(gen_world, made.listing_id) == ("blocked", "human_task_open")
    assert _runs(gen_world, made.listing_id) == runs
    (task,) = _tasks(gen_world, made.listing_id)
    assert task[1:3] == ("missing_fact", "open")
    # the content PATCH that fixes it closes the row `human_edit` and cancels the task
    set_status(gen_world, made.listing_id, "needs_review")
    await _patch_fit(api, gen_world, made)
    fit = [i for i in _issues(gen_world, made.listing_id) if i[0] == "attributes.fit"]
    assert fit[0][3:5] == ("resolved", "human_edit")
    (task,) = _tasks(gen_world, made.listing_id)
    assert task[2:] == ("cancelled", "issues_closed")


async def test_validate_replaced_definition_closes_the_row(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    runs = _runs(gen_world, made.listing_id)
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made)
    store(gen_world.conn, PERMISSIVE, version="UFAKE0002")
    await _validate(api, gen_world, made)
    assert _status(gen_world, made.listing_id) == ("validated", None)
    ((_f, _c, _s, status, resolution, _t, _n),) = _issues(gen_world, made.listing_id)
    assert (status, resolution) == ("resolved", "revalidated_clean")
    assert _runs(gen_world, made.listing_id) == runs


async def test_validate_on_approved_voids_the_approval(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, "approved")
    with gen_world.conn.transaction():
        gen_world.conn.execute("SET LOCAL session_replication_role = replica")
        gen_world.conn.execute(
            "UPDATE listings SET approved_version_id = current_version_id WHERE id = %s",
            [made.listing_id],
        )
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made)
    row = gen_world.listing(made.listing_id)
    assert (row["status"], row["status_reason"], row["approved_version_id"]) == (
        "blocked",
        "validation_errors",
        None,
    )


async def test_validate_replay_writes_nothing(api: httpx.AsyncClient, gen_world: GenWorld) -> None:
    made = await generated(gen_world)
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made)
    before = (
        _issues(gen_world, made.listing_id),
        _runs(gen_world, made.listing_id),
        gen_world.listing(made.listing_id)["last_event_at"],
    )
    await _validate(api, gen_world, made)
    after = (
        _issues(gen_world, made.listing_id),
        _runs(gen_world, made.listing_id),
        gen_world.listing(made.listing_id)["last_event_at"],
    )
    assert after == before  # occurrences unchanged, no run, no transition


async def test_validate_past_version_skips_the_check(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    set_status(gen_world, made.listing_id, "needs_review")
    await _patch_fit(api, gen_world, made)
    status = _status(gen_world, made.listing_id)
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made, listing_version_id=str(made.version_id))
    assert _issues(gen_world, made.listing_id) == []
    assert _status(gen_world, made.listing_id) == status


async def test_schema_sync_never_closes_an_adapter_row(
    api: httpx.AsyncClient, gen_world: GenWorld
) -> None:
    made = await generated(gen_world)
    await open_issue(
        gen_world,
        made,
        source="amazon_adapter",
        field_path="attributes.fit",
        error_code="MISSING_ATTRIBUTE",
        severity="error",
    )
    store(gen_world.conn, UNFED)
    await _validate(api, gen_world, made)
    store(gen_world.conn, PERMISSIVE, version="UFAKE0002")
    await _validate(api, gen_world, made)
    (adapter_row,) = _issues(gen_world, made.listing_id, "amazon_adapter")
    assert adapter_row[3] == "open"
