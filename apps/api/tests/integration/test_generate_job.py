"""S18 T16: the `listing_generate` body against the stub pipeline, as app_rw."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from .conftest import LOCAL_HOSTS
from .generation_support import GenWorld, gen_world, make_generating

pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)
__all__ = ["gen_world"]


async def _run(world: GenWorld, instructions: str | None = None, sku: str = "KURTA-002") -> Any:
    from app.workers.tasks import register_bodies
    from app.workers.wrapper import run_job

    accepted = await make_generating(world, sku=sku, instructions=instructions)
    register_bodies()
    await run_job(str(accepted.job_id))
    return accepted


def _job(world: GenWorld, job_id: uuid.UUID) -> dict[str, Any]:
    cur = world.conn.execute("SELECT * FROM job_runs WHERE id = %s", [job_id])
    names = [d.name for d in cur.description or ()]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))


async def test_generate_happy_path(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world)
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    assert listing["status"] == "validated"
    assert listing["current_version_id"] is not None
    assert job["status"] == "succeeded"
    assert gen_world.count("listing_versions", "listing_id = %s", [accepted.listing_id]) == 1
    assert gen_world.count("validation_runs", "listing_id = %s", [accepted.listing_id]) >= 1
    assert gen_world.count("agent_runs", "job_id = %s", [accepted.job_id]) >= 1


async def test_generate_blocked_after_3(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world, "stub:blocked")
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    version = gen_world.conn.execute(
        "SELECT attempts_used FROM listing_versions WHERE listing_id = %s",
        [accepted.listing_id],
    ).fetchone()
    assert listing["status"] == "blocked"
    assert listing["status_reason"] == "corrections_exhausted"
    assert version is not None and version[0] == 3
    assert job["status"] == "succeeded"
    assert job["error_report_json"] is not None


async def test_generate_failed(gen_world: GenWorld) -> None:
    accepted = await _run(gen_world, "stub:failed")
    listing = gen_world.listing(accepted.listing_id)
    job = _job(gen_world, accepted.job_id)
    assert gen_world.count("listing_versions", "job_id = %s", [accepted.job_id]) == 0
    assert job["status"] == "failed"
    assert job["error_code"] == "GENERATION_FAILED"
    assert listing["status"] == "failed"
    assert listing["status_reason"] == "generation_failed"


# ---- failure accounting: a failure that spent nothing gives the reservation back ----


def _reserved_and_report(world: GenWorld, job_id: uuid.UUID) -> tuple[Any, Any, Any, Any]:
    job = _job(world, job_id)
    return job["status"], job["error_code"], job["cost_reserved_usd"], job["error_report_json"]


async def test_generate_transient_embedding_failure_releases_reservation(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brand retrieval's query embed is down: no model stage started, so the job fails as a
    retryable MODEL_UNAVAILABLE and its reservation is zeroed (no lock-out for the day)."""
    from decimal import Decimal

    from app.services import pipeline_deps
    from mkos_schemas.codes import ErrorCode

    async def down(self: Any, *_: Any, **__: Any) -> Any:
        raise pipeline_deps.SeamPortError(ErrorCode.MODEL_UNAVAILABLE, "embedding", retryable=True)

    monkeypatch.setattr(pipeline_deps.BackendPipelineDeps, "retrieve_brand_chunks", down)
    accepted = await _run(gen_world)
    status, code, reserved, report = _reserved_and_report(gen_world, accepted.job_id)
    assert (status, code, reserved) == ("failed", "MODEL_UNAVAILABLE", Decimal("0"))
    assert report["retryable"] is True
    assert [(e["retry_class"], e["source"]) for e in report["errors"]] == [
        ("auto_retry", "ai_model")
    ]
    assert gen_world.count("listing_versions", "job_id = %s", [accepted.job_id]) == 0
    listing = gen_world.listing(accepted.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")


async def test_generate_pipeline_error_after_a_model_stage_releases_reservation(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after a writer started may follow paid calls whose records are lost with the
    exception: INTERNAL_ERROR, and (SI10 D6) the reservation is released all the same; the key's
    credit limit is the backstop for the unrecorded calls."""
    from app.workers import generation
    from mkos_schemas.enums import PipelineStage

    async def crash(_ctx: Any, deps: Any) -> Any:
        await deps.emit_node_event(PipelineStage.LOAD_CONTEXT, "started")
        await deps.emit_node_event(PipelineStage.CONTENT_AGENT, "started")
        raise RuntimeError("boom")

    monkeypatch.setattr(generation, "run_listing_pipeline", crash)
    accepted = await _run(gen_world)
    status, code, reserved, report = _reserved_and_report(gen_world, accepted.job_id)
    assert (status, code) == ("failed", "INTERNAL_ERROR")
    assert reserved == 0 and report["retryable"] is False


async def test_generate_pipeline_error_before_a_model_stage_releases_reservation(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    from app.workers import generation
    from mkos_schemas.enums import PipelineStage

    async def crash(_ctx: Any, deps: Any) -> Any:
        await deps.emit_node_event(PipelineStage.LOAD_CONTEXT, "started")
        await deps.emit_node_event(PipelineStage.RETRIEVE_BRAND, "started")
        raise RuntimeError("boom")

    monkeypatch.setattr(generation, "run_listing_pipeline", crash)
    accepted = await _run(gen_world)
    status, code, reserved, _ = _reserved_and_report(gen_world, accepted.job_id)
    assert (status, code, reserved) == ("failed", "INTERNAL_ERROR", Decimal("0"))


async def test_generate_no_version_after_transient_runs_is_model_unavailable(
    gen_world: GenWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every agent run of a no-version result timed out: a retryable MODEL_UNAVAILABLE (not the
    seller-facing GENERATION_FAILED), with the returned agent runs persisted."""
    from decimal import Decimal

    from app.workers import generation
    from mkos_pipeline import run_listing_pipeline as stub

    async def timed_out(ctx: Any, deps: Any) -> Any:
        result = await stub(ctx, deps)
        runs = [r.model_copy(update={"status": "timeout"}) for r in result.agent_runs]
        return result.model_copy(update={"agent_runs": runs})

    monkeypatch.setattr(generation, "run_listing_pipeline", timed_out)
    accepted = await _run(gen_world, "stub:failed")
    status, code, reserved, report = _reserved_and_report(gen_world, accepted.job_id)
    assert (status, code, reserved) == ("failed", "MODEL_UNAVAILABLE", Decimal("0"))
    assert report["retryable"] is True
    assert gen_world.count("agent_runs", "job_id = %s AND status = 'timeout'", [accepted.job_id])
    listing = gen_world.listing(accepted.listing_id)
    assert (listing["status"], listing["status_reason"]) == ("failed", "generation_failed")
