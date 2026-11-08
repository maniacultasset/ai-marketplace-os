"""SI13 T12-T13 (AC10-AC13, AC18; P:B2, D2-D3; CP-D1 to CP-D3): the diagnosis scheduler and job on
the stub pipeline. A proposal is drafted per unmapped code through the SI10 budget, every model
call persists its AgentRun, an unsafe draft keeps no text, and nothing a seller reads changes."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from . import factories as f
from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world
from .review_support import OPERATOR, generated, run_job

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]

CODE = "77777"


@pytest.fixture(autouse=True)
def no_proposals(gen_world: GenWorld) -> None:
    """The proposal table is platform-global: each test starts with none."""
    gen_world.conn.execute("DELETE FROM marketplace_error_proposals")


@pytest.fixture
def diagnosis_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("MARKETPLACE_ERROR_DIAGNOSIS_ENABLED", "true")
    get_settings.cache_clear()


async def _unknown(world: GenWorld, slug: str = "acme", code: str = CODE, **over: Any) -> Any:
    if slug == "acme":
        made = await generated(world, OPERATOR)
        listing_id = made.listing_id
    else:
        made = None
        f.facts_guard_off(world.conn, "products")
        product = f.product(world.conn, world.tenant(slug))
        listing_id = f.listing(world.conn, world.tenant(slug), product)
    issue = f.listing_issue(
        world.conn,
        world.tenant(slug),
        listing_id,
        source="amazon_adapter",
        field_path="listing",
        error_code="MARKETPLACE_UNKNOWN_ERROR",
        error_class="unknown",
        retry_class="user_action",
        message="Something unusual went wrong with this listing.",
        marketplace_code=code,
        dedupe_key=f"amazon_adapter|listing|MARKETPLACE_UNKNOWN_ERROR|{code}|{uuid.uuid4()}",
        **over,
    )
    return made, issue


def _proposals(world: GenWorld) -> list[dict[str, Any]]:
    cur = world.conn.execute("SELECT * FROM marketplace_error_proposals ORDER BY created_at, id")
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _jobs(world: GenWorld) -> list[tuple[Any, ...]]:
    return world.conn.execute(
        "SELECT id, tenant_id, status, max_attempts FROM job_runs "
        "WHERE entity_type = 'error_proposal' ORDER BY queued_at, id"
    ).fetchall()


async def _schedule() -> int:
    from app.workers.periodic import schedule_error_diagnosis

    return await schedule_error_diagnosis()


# ---- AC10: one proposal, its records, the reservation settled ----


async def test_one_code_one_proposal_and_its_records(
    gen_world: GenWorld, diagnosis_on: None
) -> None:
    _made, issue = await _unknown(gen_world)
    assert await _schedule() == 1
    ((job_id, tenant, status, max_attempts),) = _jobs(gen_world)
    assert (tenant, status, max_attempts) == (gen_world.tenant("acme"), "queued", 1)
    (row,) = _proposals(gen_world)
    assert (row["status"], row["raw_value"], row["sample_issue_id"]) == ("pending", CODE, issue)
    assert "tenant_id" not in row
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert row["status"] == "proposed" and row["plain_cause"] and row["lane_class"] == "seller"
    assert row["prompt_version"] == "error-diagnosis-1"
    runs = gen_world.conn.execute(
        "SELECT tenant_id, agent_name, trigger FROM agent_runs WHERE job_id = %s", [job_id]
    ).fetchall()
    assert runs == [(gen_world.tenant("acme"), "correction", "correct")]
    reserved = gen_world.conn.execute(
        "SELECT cost_reserved_usd FROM job_runs WHERE id = %s", [job_id]
    ).fetchone()
    assert reserved is not None and reserved[0] == 0
    assert await _schedule() == 0  # a proposed code is never scheduled again


async def test_the_call_sees_build_input_only(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import error_diagnosis

    seen: dict[str, Any] = {}
    real = error_diagnosis.call_complete_json

    async def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(error_diagnosis, "call_complete_json", spy)
    made, _ = await _unknown(gen_world)
    await _schedule()
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    sku = gen_world.conn.execute(
        "SELECT seller_sku FROM listing_items WHERE listing_id = %s", [made.listing_id]
    ).fetchone()
    assert seen["data"]["marketplace_code"] == CODE
    assert sku is None or sku[0] not in str(seen["data"])
    assert (seen["agent_name"], seen["trigger"], seen["max_tries"]) == ("correction", "correct", 1)


# ---- AC11: budget, model failures, an absent seam ----


async def test_a_reserve_refusal_makes_no_call(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import budget, error_diagnosis

    async def refused(*args: Any, **kwargs: Any) -> bool:
        raise budget.BudgetRefused("tenant")

    async def never(**kwargs: Any) -> Any:
        raise AssertionError("no call")

    await _unknown(gen_world)
    monkeypatch.setattr(budget, "reserve", refused)
    monkeypatch.setattr(error_diagnosis, "call_complete_json", never)
    await _schedule()
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert (row["status"], row["failure_reason"], row["tries"]) == ("failed", "budget", 0)
    assert row["plain_cause"] is None


async def test_a_budget_refusal_is_retried_by_a_new_job(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 1 fix: the next pass after a reserve refusal enqueues a fresh job (same `tries`)
    that runs to a final state; the old key would hand back the finished job and strand it."""
    from datetime import UTC, datetime, timedelta

    from app.workers import budget
    from app.workers.periodic import schedule_error_diagnosis

    real = budget.reserve

    async def refused(*args: Any, **kwargs: Any) -> bool:
        raise budget.BudgetRefused("tenant")

    await _unknown(gen_world)
    monkeypatch.setattr(budget, "reserve", refused)
    first = datetime.now(UTC)
    assert await schedule_error_diagnosis(first) == 1
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert (row["status"], row["failure_reason"], row["tries"]) == ("failed", "budget", 0)
    gen_world.conn.execute(
        "UPDATE marketplace_error_proposals SET next_try_at = now() - interval '1 minute'"
    )
    # the same pass time names the old job: the claim rolls back, nothing is counted
    assert await schedule_error_diagnosis(first) == 0
    (row,) = _proposals(gen_world)
    assert row["status"] == "failed" and len(_jobs(gen_world)) == 1
    monkeypatch.setattr(budget, "reserve", real)
    assert await schedule_error_diagnosis(first + timedelta(minutes=10)) == 1
    jobs = _jobs(gen_world)
    assert len(jobs) == 2 and jobs[1][2] == "queued"
    await run_job(jobs[1][0])
    (row,) = _proposals(gen_world)
    assert row["status"] == "proposed" and row["plain_cause"]


async def test_model_failures_count_to_three(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.repositories.error_proposals import ErrorProposalRepository
    from app.repositories.uow import tenant_uow
    from app.workers import error_diagnosis

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("model down")

    monkeypatch.setattr(error_diagnosis, "call_complete_json", boom)
    _made, issue = await _unknown(gen_world)
    for expected in (1, 2, 3):
        gen_world.conn.execute(
            "UPDATE marketplace_error_proposals SET next_try_at = now() - interval '1 second'"
        )
        assert await _schedule() == 1
        job_id = _jobs(gen_world)[-1][0]
        await run_job(job_id)
        (row,) = _proposals(gen_world)
        assert (row["status"], row["failure_reason"], row["tries"]) == (
            "failed",
            "model_failed",
            expected,
        )
    gen_world.conn.execute(
        "UPDATE marketplace_error_proposals SET next_try_at = now() - interval '1 second'"
    )
    assert await _schedule() == 0
    async with tenant_uow(gen_world.tenant("acme")) as uow:
        assert await ErrorProposalRepository(uow.session).claim("amazon", CODE, issue) is None


async def test_an_absent_seam_is_not_available(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import error_diagnosis

    monkeypatch.setattr(error_diagnosis, "seam_entry", lambda: None)
    await _unknown(gen_world)
    await _schedule()
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert (row["status"], row["failure_reason"], row["tries"]) == ("failed", "not_available", 1)
    runs = gen_world.conn.execute("SELECT count(*) FROM agent_runs WHERE job_id = %s", [job_id])
    assert runs.fetchone() == (0,)


# ---- AC12, AC13: an unsafe draft keeps no text; nothing a seller reads changes ----


async def test_a_url_reply_is_invalid_with_no_text(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.resolution.diagnosis import DiagnosisOut
    from app.workers import error_diagnosis

    real = error_diagnosis.call_complete_json

    async def unsafe(**kwargs: Any) -> Any:
        await real(**kwargs)  # the stub's record
        return DiagnosisOut.model_validate(
            {
                "plain_cause": "See https://evil.example/fix for what Amazon wants here.",
                "lane_class": "seller",
                "suggested_fix": "Open the link and follow the steps listed there.",
            }
        )

    monkeypatch.setattr(error_diagnosis, "call_complete_json", unsafe)
    await _unknown(gen_world)
    await _schedule()
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert row["status"] == "invalid" and row["plain_cause"] is None
    assert row["suggested_fix"] is None and row["lane_class"] is None


async def test_a_proposal_reaches_no_seller(
    api: httpx.AsyncClient, gen_world: GenWorld, diagnosis_on: None
) -> None:
    made, issue = await _unknown(gen_world)
    maps = gen_world.conn.execute("SELECT count(*) FROM marketplace_error_map").fetchone()
    await _schedule()
    ((job_id, *_),) = _jobs(gen_world)
    await run_job(job_id)
    (row,) = _proposals(gen_world)
    assert row["status"] == "proposed"
    page = await api.get(
        "/api/v1/issues",
        params={"listing_id": str(made.listing_id)},
        headers=gen_world.headers(OPERATOR),
    )
    assert page.status_code == 200, page.text
    assert row["plain_cause"] not in page.text and row["suggested_fix"] not in page.text
    listing = await api.get(
        f"/api/v1/listings/{made.listing_id}", headers=gen_world.headers(OPERATOR)
    )
    assert row["plain_cause"] not in listing.text
    unchanged = gen_world.conn.execute(
        "SELECT error_class, error_map_id FROM listing_issues WHERE id = %s", [issue]
    ).fetchone()
    assert unchanged == ("unknown", None)
    assert gen_world.conn.execute("SELECT count(*) FROM marketplace_error_map").fetchone() == maps


async def test_a_dead_job_fails_the_pending_proposal(gen_world: GenWorld) -> None:
    from app.services.job_hooks import on_embed_terminal

    pid = gen_world.conn.execute(
        "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status) "
        "VALUES ('amazon', 'code', '55555', 'pending') RETURNING id"
    ).fetchone()
    assert pid is not None
    proposal_id = pid[0]

    class Job:
        entity_type = "error_proposal"
        entity_id = proposal_id
        status = "dead_letter"
        tenant_id = gen_world.tenant("acme")

    from app.repositories.uow import tenant_uow

    async with tenant_uow(gen_world.tenant("acme")) as uow:
        await on_embed_terminal(uow.session, Job(), None)  # type: ignore[arg-type]
    row = gen_world.conn.execute(
        "SELECT status, tries FROM marketplace_error_proposals WHERE id = %s", [proposal_id]
    ).fetchone()
    assert row == ("failed", 1)


# ---- T13: the scheduling ----


async def test_two_tenants_one_proposal_billed_to_the_newest(
    gen_world: GenWorld, diagnosis_on: None
) -> None:
    await _unknown(gen_world, "bharat")
    await _unknown(gen_world, "acme")
    assert await _schedule() == 1
    ((_job, tenant, _s, _m),) = _jobs(gen_world)
    assert tenant == gen_world.tenant("acme")
    assert await _schedule() == 0
    other = gen_world.conn.execute(
        "SELECT count(*) FROM agent_runs WHERE tenant_id = %s AND agent_name = 'correction'",
        [gen_world.tenant("bharat")],
    ).fetchone()
    assert other == (0,)


async def test_seven_codes_take_two_passes(gen_world: GenWorld, diagnosis_on: None) -> None:
    for n in range(7):
        await _unknown(gen_world, code=f"7000{n}")
    assert await _schedule() == 5
    assert await _schedule() == 2
    assert await _schedule() == 0


async def test_flag_off_writes_nothing(gen_world: GenWorld) -> None:
    await _unknown(gen_world)
    assert await _schedule() == 0
    assert _proposals(gen_world) == [] and _jobs(gen_world) == []


async def test_admission_refusal_writes_nothing(
    gen_world: GenWorld, diagnosis_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import budget

    real = budget.admission

    async def refused(*args: Any, **kwargs: Any) -> Any:
        return "tenant"

    monkeypatch.setattr(budget, "admission", refused)
    await _unknown(gen_world)
    assert await _schedule() == 0 and _proposals(gen_world) == []
    monkeypatch.setattr(budget, "admission", real)
    assert await _schedule() == 1


async def test_a_retry_waits_for_its_backoff(gen_world: GenWorld, diagnosis_on: None) -> None:
    await _unknown(gen_world)
    gen_world.conn.execute(
        "INSERT INTO marketplace_error_proposals (marketplace, match_on, raw_value, status, tries) "
        "VALUES ('amazon', 'code', %s, 'pending', 0)",
        [CODE],
    )
    gen_world.conn.execute(
        "UPDATE marketplace_error_proposals SET status = 'failed', tries = 1, "
        "failure_reason = 'model_failed', next_try_at = now() + interval '1 hour'"
    )
    assert await _schedule() == 0


async def test_two_schedulers_at_once_make_one_job(gen_world: GenWorld, diagnosis_on: None) -> None:
    await _unknown(gen_world)
    results = await asyncio.gather(_schedule(), _schedule(), return_exceptions=True)
    assert sum(r for r in results if isinstance(r, int)) == 1
    assert len(_proposals(gen_world)) == 1 and len(_jobs(gen_world)) == 1


async def test_the_queue_entry_carries_only_the_timestamp() -> None:
    from app.workers.periodic import PERIODIC

    cron, task, pass_context = PERIODIC["schedule_error_diagnosis"]
    assert cron == "*/10 * * * *" and pass_context is False
    import inspect

    assert list(inspect.signature(task).parameters) == ["timestamp"]
