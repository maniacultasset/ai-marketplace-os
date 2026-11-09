"""SI7 T11-T12: the candidate job through the real wrapper, on a fake object store.

Part 1 (T11): the zero-token kinds (`fix`, `size_chart`), the terminal and requeue hooks."""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from app.adapters.storage import StorageUnavailable
from app.services import image_runner
from app.services.image_keys import candidate_key
from app.services.image_runner import CandidateChildFailed
from PIL import Image as PILImage

from .candidates_support import (
    agent_run_count,
    candidate_row,
    chart_product,
    enqueue_candidate,
    photo,
    source_main,
    use_candidate_storage,
)
from .conftest import LOCAL_HOSTS
from .images_support import FakeStorage, images_db, run_image_job
from .jobs_support import JobWorld, job_row

__all__ = ["images_db"]
pytestmark = pytest.mark.allow_hosts(LOCAL_HOSTS)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    use_candidate_storage(monkeypatch, fake)
    return fake


async def _fix(jw: JobWorld, store: FakeStorage, **kw: Any) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = jw.tenant("acme")
    p, src, digest = source_main(jw.conn, store, tenant, **kw)
    return await enqueue_candidate(tenant, p, src, digest)


async def test_fix_on_an_off_white_main_is_ready(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready" and row["failure_reason"] is None
    tenant = images_db.tenant("acme")
    key = candidate_key(tenant, row["product_id"], candidate)
    assert row["storage_key"] == key and key in store.objects
    img = PILImage.open(io.BytesIO(store.objects[key][0]))
    assert img.format == "JPEG" and img.width == img.height >= 1000
    assert (row["width_px"], row["height_px"]) == img.size
    assert agent_run_count(images_db.conn, tenant) == 0
    assert job_row(images_db.conn, job)["status"] == "succeeded"


async def test_fix_too_small(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store, clean=photo((600, 600)))
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "too_small")


async def test_size_chart_is_ready(images_db: JobWorld, store: FakeStorage) -> None:
    from app.services.image_candidates import size_chart_source

    tenant = images_db.tenant("acme")
    p = chart_product(images_db.conn, tenant)
    found = images_db.conn.execute("SELECT attributes FROM products WHERE id = %s", [p]).fetchone()
    assert found is not None
    source = size_chart_source(found[0])
    assert source is not None
    candidate, job = await enqueue_candidate(tenant, p, None, source.source_hash, kind="size_chart")
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready" and (row["width_px"], row["height_px"]) == (1500, 1500)
    assert agent_run_count(images_db.conn, tenant) == 0


async def test_size_chart_changed_since_create(images_db: JobWorld, store: FakeStorage) -> None:
    tenant = images_db.tenant("acme")
    p = chart_product(images_db.conn, tenant)
    candidate, job = await enqueue_candidate(tenant, p, None, "0" * 64, kind="size_chart")
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_changed")


async def test_source_replaced_before_the_job(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    images_db.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = ("
        "SELECT source_image_id FROM image_candidates WHERE id = %s)",
        ["b" * 64, candidate],
    )
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_changed")


async def test_storage_outage_is_retried(images_db: JobWorld, store: FakeStorage) -> None:
    candidate, job = await _fix(images_db, store)
    real_get = store.get
    calls = {"n": 0}

    async def flaky(*a: Any, **k: Any) -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise StorageUnavailable("down")
        return await real_get(*a, **k)

    store.get = flaky  # type: ignore[method-assign]
    runs = await run_image_job(job, max_runs=3)
    assert runs == 2
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"


async def test_child_failure_on_every_attempt_is_model_failed_and_not_cached(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*a: Any, **k: Any) -> Any:
        raise CandidateChildFailed("timeout")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", broken)
    tenant = images_db.tenant("acme")
    p, src, digest = source_main(images_db.conn, store, tenant)
    candidate, job = await enqueue_candidate(tenant, p, src, digest)
    await run_image_job(job, max_runs=3)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert job_row(images_db.conn, job)["status"] == "dead_letter"
    again, _ = await enqueue_candidate(tenant, p, src, digest)  # not cached: a new row and job
    assert again != candidate


async def test_framed_output_failing_background_is_not_white(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.image_runner import CandidateReply

    async def verdict(*a: Any, **k: Any) -> CandidateReply:
        return CandidateReply(ok=False, reason="not_white")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", verdict)
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "not_white")


async def test_body_exception_is_model_failed(
    images_db: JobWorld, store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(image_runner, "run_candidate_isolated", boom)
    candidate, job = await _fix(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert job_row(images_db.conn, job)["status"] == "failed"


async def test_cancel_is_model_failed(images_db: JobWorld, store: FakeStorage) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository
    from app.services.job_hooks import run_terminal_hook

    candidate, job = await _fix(images_db, store)
    tenant = images_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        cancelled = await JobRunRepository(session, tenant).cancel_queued(job)
        assert cancelled is not None
        await run_terminal_hook(session, cancelled, None)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")


async def test_staff_requeue_refused(images_db: JobWorld, store: FakeStorage) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository, NotEligibleError
    from app.services.job_hooks import run_requeue_hook

    candidate, job = await _fix(images_db, store)
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 3, started_at = now(), "
        "lease_expires_at = now() + interval '1 hour' WHERE id = %s",
        [job],
    )
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'dead_letter', lease_expires_at = NULL, "
        "finished_at = now(), error_report_json = '{\"errors\": []}' WHERE id = %s",
        [job],
    )
    tenant = images_db.tenant("acme")
    with pytest.raises(NotEligibleError):
        async with unit_of_work(tenant) as session:  # the staff CLI: requeue, then the hook
            old, new = await JobRunRepository(session, tenant).requeue(job)
            await run_requeue_hook(session, old, new)
    count = images_db.conn.execute(
        "SELECT count(*) FROM job_runs WHERE parent_job_id = %s", [job]
    ).fetchone()
    assert count == (0,)  # the transaction rolled back
    assert candidate_row(images_db.conn, candidate)["status"] == "queued"


async def test_old_jobs_hook_leaves_a_newer_jobs_row(
    images_db: JobWorld, store: FakeStorage
) -> None:
    from app.db.session import unit_of_work
    from app.repositories.jobs import JobRunRepository
    from app.services.job_hooks import run_terminal_hook

    candidate, job = await _fix(images_db, store)
    newer = uuid.uuid4()
    images_db.conn.execute(
        "UPDATE image_candidates SET job_id = %s WHERE id = %s", [newer, candidate]
    )
    tenant = images_db.tenant("acme")
    async with unit_of_work(tenant) as session:
        cancelled = await JobRunRepository(session, tenant).cancel_queued(job)
        assert cancelled is not None
        await run_terminal_hook(session, cancelled, None)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "queued" and row["job_id"] == newer


# ---- Part 2 (T12): the AI white-background path, on the stub pipeline and seam fakes ----

import logging  # noqa: E402
from collections.abc import Callable  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.workers import image_edit_seam  # noqa: E402
from mkos_schemas.image_edit import ImageEditRequest, ImageEditResult  # noqa: E402
from mkos_schemas.jobs import ModelCredentials  # noqa: E402

from .candidates_support import edit_on  # noqa: E402

__all__ += ["edit_on"]
Seam = Callable[[ImageEditRequest, ModelCredentials], Any]


async def _edit(
    jw: JobWorld, store: FakeStorage, *, bg: tuple[int, int, int] = (245, 245, 245), **kw: Any
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = jw.tenant("acme")
    p, src, digest = source_main(jw.conn, store, tenant, clean=photo(bg=bg), **kw)
    candidate, job = await enqueue_candidate(tenant, p, src, digest, kind="white_background")
    return candidate, job, p


def _stub_with(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[ImageEditResult, ImageEditRequest], Any]
) -> list[ImageEditRequest]:
    """The stub pipeline's answer, changed by `change` (a seam fake); records each request."""
    from mkos_pipeline import edit_image as stub

    seen: list[ImageEditRequest] = []

    async def fake(request: ImageEditRequest, credentials: ModelCredentials) -> Any:
        seen.append(request)
        return change(await stub(request, credentials), request)

    monkeypatch.setattr(image_edit_seam, "call_edit_image", fake)
    return seen


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The app's structured logging on the root logger, captured."""
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings())
    caplog.set_level(logging.DEBUG)
    return caplog


def _reserved(conn: Any, job: uuid.UUID) -> Decimal:
    row = conn.execute("SELECT cost_reserved_usd FROM job_runs WHERE id = %s", [job]).fetchone()
    assert row is not None
    return Decimal(row[0])


def _runs(conn: Any, job: uuid.UUID) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM agent_runs WHERE job_id = %s", [job])
    names = [d.name for d in cur.description or ()]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


async def test_stub_echo_on_off_white_is_ready_with_one_run(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    from mkos_pipeline import edit_image as stub

    candidate, job, p = await _edit(images_db, store)
    during: list[Decimal] = []

    async def spy(request: ImageEditRequest, credentials: ModelCredentials) -> Any:
        during.append(_reserved(images_db.conn, job))
        return await stub(request, credentials)

    edit_on.setattr(image_edit_seam, "call_edit_image", spy)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready", row["failure_reason"]
    assert during == [Decimal("0.10")] and _reserved(images_db.conn, job) == 0
    (run,) = _runs(images_db.conn, job)
    assert (run["agent_name"], run["trigger"], run["prompt_version"]) == (
        "image",
        "vision",
        "image_edit/white_bg/v1",
    )
    assert run["cost_usd"] == Decimal("0.039000") and run["product_id"] == p
    assert row["agent_run_id"] == run["id"] and row["cost_usd"] == Decimal("0.039000")
    assert row["fidelity_json"] is not None and row["served_provider"] == "stub"
    assert job_row(images_db.conn, job)["max_attempts"] == 1


def _shifted(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
    img = PILImage.open(io.BytesIO(__import__("base64").b64decode(request.image.data_b64)))
    moved = PILImage.new("RGB", img.size, (255, 255, 255))
    moved.paste(img.crop((0, 0, img.width - img.width // 10, img.height)), (img.width // 10, 0))
    out = io.BytesIO()
    moved.save(out, format="PNG")
    return result.model_copy(
        update={
            "image_b64": __import__("base64").b64encode(out.getvalue()).decode(),
            "mime": "image/png",
        }
    )


def _halved(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
    """The model drew the product at half its size: beyond the registration's scale range."""
    data = __import__("base64").b64decode(request.image.data_b64)
    img = PILImage.open(io.BytesIO(data)).convert("RGB")
    small = PILImage.new("RGB", img.size, (255, 255, 255))
    half = img.resize((img.width // 2, img.height // 2))
    small.paste(half, (img.width // 4, img.height // 4))
    out = io.BytesIO()
    small.save(out, format="PNG")
    return result.model_copy(
        update={
            "image_b64": __import__("base64").b64encode(out.getvalue()).decode(),
            "mime": "image/png",
        }
    )


async def test_shifted_edit_is_registered_and_ready(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """A model that moved the product a tenth of the frame: the candidate is registered back
    onto the seller's photo and built from its pixels."""
    candidate, job, _ = await _edit(images_db, store)
    _stub_with(edit_on, _shifted)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready", row["failure_reason"]
    assert len(_runs(images_db.conn, job)) == 1


def _recoloured(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
    img = PILImage.open(io.BytesIO(__import__("base64").b64decode(request.image.data_b64)))
    r, g, b = img.convert("RGB").split()
    swapped = PILImage.merge("RGB", (b, r, g))
    out = io.BytesIO()
    swapped.save(out, format="PNG")
    return result.model_copy(
        update={
            "image_b64": __import__("base64").b64encode(out.getvalue()).decode(),
            "mime": "image/png",
        }
    )


@pytest.mark.parametrize("change", [_halved, _recoloured], ids=["halved", "recoloured"])
async def test_unfaithful_twice_fails_fidelity_with_a_run_per_try(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch, change: Any
) -> None:
    """Two unfaithful edits: one retry of the same input, then the same refusal as before."""
    candidate, job, _ = await _edit(images_db, store)
    seen = _stub_with(edit_on, change)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "fidelity")
    assert row["fidelity_json"] is not None
    assert len(seen) == 2 and seen[0] == seen[1]
    runs = _runs(images_db.conn, job)
    assert len(runs) == 2 and len({r["id"] for r in runs}) == 2
    assert row["cost_usd"] == sum(r["cost_usd"] for r in runs)
    assert row["agent_run_id"] in {r["id"] for r in runs}
    assert _reserved(images_db.conn, job) == 0


def _first_halved(tries: list[int]) -> Callable[[ImageEditResult, ImageEditRequest], Any]:
    def change(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
        tries.append(1)
        return _halved(result, request) if len(tries) == 1 else result

    return change


async def test_unfaithful_then_faithful_is_ready(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """A first unfaithful edit, then a faithful one: the retry's image is the candidate."""
    candidate, job, _ = await _edit(images_db, store)
    seen = _stub_with(edit_on, _first_halved([]))
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert row["status"] == "ready", row["failure_reason"]
    assert len(seen) == 2 and seen[0] == seen[1]
    runs = _runs(images_db.conn, job)
    assert len(runs) == 2 and row["cost_usd"] == Decimal("0.078000")
    assert row["agent_run_id"] in {r["id"] for r in runs}
    assert _reserved(images_db.conn, job) == 0
    assert row["storage_key"] in store.objects


async def test_fidelity_retry_does_not_count_again_toward_the_daily_limit(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """A daily limit of one candidate: the retry is part of that one candidate."""
    from app.core.config import get_settings

    edit_on.setenv("IMAGE_EDIT_TENANT_DAILY_LIMIT", "1")
    get_settings.cache_clear()
    tries: list[int] = []
    candidate, job, _ = await _edit(images_db, store)
    _stub_with(edit_on, _first_halved(tries))
    await run_image_job(job)
    assert candidate_row(images_db.conn, candidate)["status"] == "ready"
    assert len(tries) == 2


async def test_fidelity_retry_refused_by_the_cap_keeps_the_first_refusal(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """A tenant cap that admits one edit (0.10) but not a second after its 0.039 spend: no
    retry call, the first refusal stands, one run."""
    images_db.conn.execute(
        "UPDATE tenants SET llm_daily_budget_usd = 0.12 WHERE id = %s", [images_db.tenant("acme")]
    )
    candidate, job, _ = await _edit(images_db, store)
    seen = _stub_with(edit_on, _halved)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "fidelity")
    assert len(seen) == 1 and len(_runs(images_db.conn, job)) == 1
    assert _reserved(images_db.conn, job) == 0


async def test_grey_source_echo_is_not_white(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    candidate, job, _ = await _edit(images_db, store, bg=(200, 200, 200))
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "not_white")


async def test_credit_exhausted(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    from mkos_pipeline import edit_image as stub

    async def credit(request: ImageEditRequest, credentials: ModelCredentials) -> Any:
        return await stub(request.model_copy(update={"category": "stub:credit"}), credentials)

    candidate, job, _ = await _edit(images_db, store)
    edit_on.setattr(image_edit_seam, "call_edit_image", credit)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "credit_used_up")
    assert _reserved(images_db.conn, job) == 0


async def test_tenant_cap_refuses_without_a_seam_call(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    candidate, job, _ = await _edit(images_db, store)
    images_db.conn.execute(
        "UPDATE tenants SET llm_daily_budget_usd = 0.01 WHERE id = %s", [images_db.tenant("acme")]
    )
    seen = _stub_with(edit_on, lambda r, q: r)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "budget")
    assert seen == [] and _runs(images_db.conn, job) == []


async def test_caps_see_edits(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    """A tenant cap of 0.12 with one stub edit (0.039) done today: the next edit is refused before the
    seam. The stub's STUB_EPOCH record is stored at today's `started_at` (clamped), and a running
    candidate job's 0.10 reservation counts in `tenant_state`."""
    from datetime import UTC, datetime

    from app.repositories.uow import tenant_uow

    tenant = images_db.tenant("acme")
    images_db.conn.execute("UPDATE tenants SET llm_daily_budget_usd = 0.12 WHERE id = %s", [tenant])
    first, job1, _ = await _edit(images_db, store)
    states: list[Decimal] = []
    from mkos_pipeline import edit_image as stub

    async def spy(request: ImageEditRequest, credentials: ModelCredentials) -> Any:
        async with tenant_uow(tenant) as uow:
            state = await uow.budget.tenant_state(datetime.now(UTC).date(), default_cap=Decimal(1))
        states.append(state.reserved)
        return await stub(request, credentials)

    edit_on.setattr(image_edit_seam, "call_edit_image", spy)
    await run_image_job(job1)
    assert candidate_row(images_db.conn, first)["status"] == "ready"
    assert states == [Decimal("0.10")]
    (run,) = _runs(images_db.conn, job1)
    assert run["started_at"].date() == datetime.now(UTC).date()
    second, job2, _ = await _edit(images_db, store)
    calls: list[Any] = []
    edit_on.setattr(image_edit_seam, "call_edit_image", lambda *a: calls.append(a))
    await run_image_job(job2)
    row = candidate_row(images_db.conn, second)
    assert (row["status"], row["failure_reason"]) == ("failed", "budget") and calls == []


async def test_pipeline_without_edit_image(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    import importlib

    real = importlib.import_module

    def without(name: str, *a: Any) -> Any:
        module = real(name, *a)
        if name == "mkos_pipeline":
            return type("Old", (), {"run_listing_pipeline": module.run_listing_pipeline})
        return module

    edit_on.setattr(importlib, "import_module", without)
    candidate, job, _ = await _edit(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert _runs(images_db.conn, job) == [] and _reserved(images_db.conn, job) == 0


@pytest.mark.parametrize("field", ["tenant_id", "job_id"])
async def test_foreign_record_is_untrusted(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch, field: str
) -> None:
    def foreign(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
        record = result.agent_runs[0].model_copy(update={field: uuid.uuid4()})
        return result.model_copy(update={"agent_runs": [record]})

    candidate, job, _ = await _edit(images_db, store)
    _stub_with(edit_on, foreign)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert _runs(images_db.conn, job) == [] and _reserved(images_db.conn, job) == Decimal("0.10")


async def test_record_ids_are_restamped_from_the_job(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    def odd(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
        record = result.agent_runs[0].model_copy(
            update={"product_id": uuid.uuid4(), "request_id": "other", "attempt": 3}
        )
        return result.model_copy(update={"agent_runs": [record]})

    _, job, p = await _edit(images_db, store)
    _stub_with(edit_on, odd)
    await run_image_job(job)
    (run,) = _runs(images_db.conn, job)
    assert run["product_id"] == p and run["attempt"] == 1 and run["request_id"] != "other"


async def test_child_crash_after_the_call_keeps_the_record_and_is_not_cached(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    real = image_runner.run_candidate_isolated

    async def crash_finish(op: str, *a: Any, **k: Any) -> Any:
        if op == "finish_white":
            raise CandidateChildFailed("exit_3")
        return await real(op, *a, **k)

    edit_on.setattr(image_runner, "run_candidate_isolated", crash_finish)
    tenant = images_db.tenant("acme")
    p, src, digest = source_main(images_db.conn, store, tenant)
    candidate, job = await enqueue_candidate(tenant, p, src, digest, kind="white_background")
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert len(_runs(images_db.conn, job)) == 1
    again, _ = await enqueue_candidate(tenant, p, src, digest, kind="white_background")
    assert again != candidate


async def test_hanging_seam_times_out(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.workers import image_candidate

    async def hang(*a: Any) -> Any:
        await asyncio.sleep(30)

    edit_on.setattr(image_edit_seam, "call_edit_image", hang)
    edit_on.setenv("LLM_TIMEOUT_SECONDS", "1")
    edit_on.setattr(image_candidate, "SEAM_GRACE_S", 0)
    from app.core.config import get_settings

    get_settings.cache_clear()
    candidate, job, _ = await _edit(images_db, store)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")


async def test_reaped_lease_is_dead_letter_never_a_second_call(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    from app.workers.periodic import reap_once

    candidate, job, _ = await _edit(images_db, store)
    images_db.conn.execute(
        "UPDATE job_runs SET status = 'running', attempt = 1, started_at = now(), "
        "lease_expires_at = now() - interval '1 second' WHERE id = %s",
        [job],
    )
    await reap_once()
    assert job_row(images_db.conn, job)["status"] == "dead_letter"
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")


async def test_no_base64_or_signature_in_logs_or_rows(
    images_db: JobWorld,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    import base64

    candidate, job, _ = await _edit(images_db, store)
    seen = _stub_with(edit_on, lambda r, q: r)
    await run_image_job(job)
    marker = seen[0].image.data_b64[100:160]
    text = logs.text + " ".join(str(r.__dict__) for r in logs.records)
    assert "image_candidate.model_done" in text
    assert marker not in text and "X-Amz-Signature" not in text
    row = candidate_row(images_db.conn, candidate)
    assert marker not in str(row["fidelity_json"])
    job_text = str(job_row(images_db.conn, job))
    assert marker not in job_text and "X-Amz-Signature" not in job_text
    assert base64.b64encode(b"\xff\xd8\xff").decode() not in str(_runs(images_db.conn, job))


async def test_served_provider_on_the_row_and_the_log(
    images_db: JobWorld,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
) -> None:
    candidate, job, _ = await _edit(images_db, store)
    _stub_with(edit_on, lambda r, q: r.model_copy(update={"served_provider": "Google"}))
    await run_image_job(job)
    assert candidate_row(images_db.conn, candidate)["served_provider"] == "Google"
    (record,) = [r for r in logs.records if "image_candidate.model_done" in r.getMessage()]
    assert "Google" in str(record.__dict__)


# ---- review fixes ----


async def test_edit_source_replaced_before_the_job_is_not_cached(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    candidate, job, _ = await _edit(images_db, store)
    images_db.conn.execute(
        "UPDATE images SET clean_content_hash = %s WHERE id = ("
        "SELECT source_image_id FROM image_candidates WHERE id = %s)",
        ["b" * 64, candidate],
    )
    seen = _stub_with(edit_on, lambda r, q: r)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "source_changed")
    assert seen == [] and _reserved(images_db.conn, job) == 0


async def test_an_untrusted_paid_edit_still_counts_toward_the_daily_limit(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from app.repositories.uow import tenant_uow

    def foreign(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
        record = result.agent_runs[0].model_copy(update={"job_id": uuid.uuid4()})
        return result.model_copy(update={"agent_runs": [record]})

    candidate, job, _ = await _edit(images_db, store)
    _stub_with(edit_on, foreign)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["agent_run_id"]) == ("failed", None)
    async with tenant_uow(images_db.tenant("acme")) as uow:
        assert await uow.image_candidates.count_ai_today(datetime.now(UTC).date()) == 1


# ---- SI10 D4/D5 (joint test, SI10 merged after SI7): the edit job and the AI pause ----


def _pause_row(conn: Any) -> tuple[Any, ...]:
    row = conn.execute("SELECT paused_reason FROM ai_credit_state WHERE id = 1").fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.parametrize(
    ("reason", "failure"), [("credit_exhausted", "credit_used_up"), ("key_invalid", "model_failed")]
)
async def test_paused_ai_refuses_the_edit_without_a_seam_call(
    images_db: JobWorld,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
    reason: str,
    failure: str,
) -> None:
    candidate, job, _ = await _edit(images_db, store)
    images_db.conn.execute(
        "UPDATE ai_credit_state SET paused_reason = %s, paused_at = now() WHERE id = 1", [reason]
    )
    seen = _stub_with(edit_on, lambda r, q: r)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", failure)
    assert seen == [] and _runs(images_db.conn, job) == []
    assert _reserved(images_db.conn, job) == 0


async def test_credit_refusal_of_an_edit_pauses_ai(
    images_db: JobWorld, store: FakeStorage, edit_on: pytest.MonkeyPatch, credit_reset: None
) -> None:
    from mkos_pipeline import edit_image as stub

    async def credit(request: ImageEditRequest, credentials: ModelCredentials) -> Any:
        return await stub(request.model_copy(update={"category": "stub:credit"}), credentials)

    candidate, job, _ = await _edit(images_db, store)
    edit_on.setattr(image_edit_seam, "call_edit_image", credit)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "credit_used_up")
    assert _pause_row(images_db.conn) == ("credit_exhausted",)


@pytest.mark.parametrize(("confirmed", "paused"), [(True, "key_invalid"), (False, None)])
async def test_key_refusal_of_an_edit_pauses_only_once_confirmed(
    images_db: JobWorld,
    store: FakeStorage,
    edit_on: pytest.MonkeyPatch,
    credit_reset: None,
    confirmed: bool,
    paused: str | None,
) -> None:
    from app.workers import budget
    from mkos_pipeline.image_edit import _report

    async def confirm(**_: Any) -> bool:
        return confirmed

    def refused(result: ImageEditResult, request: ImageEditRequest) -> ImageEditResult:
        from mkos_schemas.codes import ErrorCode

        error = _report(request, ErrorCode.AI_PROVIDER_KEY_INVALID, "The key was refused.")
        return ImageEditResult(status="failed", error=error)

    candidate, job, _ = await _edit(images_db, store)
    edit_on.setattr(budget, "confirm_key_refusal", confirm)
    _stub_with(edit_on, refused)
    await run_image_job(job)
    row = candidate_row(images_db.conn, candidate)
    assert (row["status"], row["failure_reason"]) == ("failed", "model_failed")
    assert _pause_row(images_db.conn) == (paused,)
