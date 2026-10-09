"""S18 T13 (AC 7; D9; T100): the trust checks on a pipeline result, pure."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from app.services.generation_result import Trusted, Untrusted, check_result, salvage_records
from hypothesis import given, settings
from hypothesis import strategies as st
from mkos_schemas.codes import ErrorCode
from mkos_schemas.enums import ErrorSource
from mkos_schemas.jobs import GenerationResult
from tests.unit.generation_builders import CLAIM, NOW, OTHER_TENANT, context, result


def _check(res: Any, ctx: Any = None) -> Trusted | Untrusted:
    return check_result(res, ctx=ctx or context(), claim_started_at=CLAIM, now=NOW)


def test_result_trusted_baseline() -> None:
    checked = _check(result(context()))
    assert isinstance(checked, Trusted)
    assert checked.listing is not None and len(checked.records) == 4


def _variant(case: str) -> Any:
    ctx = context()
    base = result(ctx)
    listing = base.listing
    assert listing is not None
    if case == "job_id":
        return base.model_copy(update={"job_id": uuid.uuid4()})
    if case == "request_id":
        return base.model_copy(update={"request_id": "req-other"})
    if case in ("product", "marketplace", "category", "locale"):
        changes: dict[str, dict[str, Any]] = {
            "product": {"product_id": uuid.uuid4()},
            "marketplace": {"marketplace": "flipkart"},
            "category": {"category": "home_decor_lamps"},
            "locale": {"locale": "hi-IN"},
        }
        value = changes[case]
        return GenerationResult.model_construct(
            **{**dict(base), "listing": listing.model_copy(update=value)}
        )
    if case == "101_records":
        records = [base.agent_runs[0].model_copy(update={"id": uuid.uuid4()}) for _ in range(101)]
        return base.model_copy(update={"agent_runs": records})
    if case == "construct_bypass":
        return GenerationResult.model_construct(**{**dict(base), "attempts_used": 7})
    raise ValueError(case)


@pytest.mark.parametrize(
    "case",
    [
        "job_id",
        "request_id",
        "product",
        "marketplace",
        "category",
        "locale",
        "101_records",
        "construct_bypass",
    ],
)
def test_result_untrusted(case: str) -> None:
    checked = _check(_variant(case))
    assert isinstance(checked, Untrusted), case
    report = checked.report
    assert report.error_code is ErrorCode.INTERNAL_ERROR
    (item,) = report.errors
    assert (item.field, item.source, item.error_code) == (
        "listing",
        ErrorSource.SYSTEM,
        ErrorCode.INTERNAL_ERROR,
    )
    assert "SCHEMA_INVALID" not in report.model_dump_json()


def test_result_extra_key_dropped() -> None:
    ctx = context()
    base = result(ctx)
    raw = base.model_dump(mode="json")
    raw["listing"]["surprise_field"] = "from a newer pipeline"
    raw["unknown_top"] = 1
    constructed = GenerationResult.model_validate(raw)
    checked = _check(constructed, ctx)
    assert isinstance(checked, Trusted)
    assert checked.listing is not None
    assert "surprise_field" not in checked.listing.model_dump_json()


def test_records_identity_from_job() -> None:
    ctx = context()
    base = result(ctx)
    forged = [
        r.model_copy(update={"tenant_id": OTHER_TENANT, "job_id": uuid.uuid4()})
        for r in base.agent_runs
    ]
    checked = _check(base.model_copy(update={"agent_runs": forged}), ctx)
    assert isinstance(checked, Trusted)
    assert {r.tenant_id for r in checked.records} == {ctx.tenant_id}
    assert {r.job_id for r in checked.records} == {ctx.job_id}
    assert {r.request_id for r in checked.records} == {ctx.request_id}


@pytest.mark.parametrize("when", ["before", "after"])
def test_started_at_clamped(when: str) -> None:
    ctx = context()
    base = result(ctx)
    shift = CLAIM - timedelta(days=2) if when == "before" else NOW + timedelta(hours=1)
    moved = [r.model_copy(update={"started_at": shift}) for r in base.agent_runs]
    checked = _check(base.model_copy(update={"agent_runs": moved}), ctx)
    assert isinstance(checked, Trusted)
    assert {r.started_at for r in checked.records} == {CLAIM}


def test_cp3_stamps() -> None:
    ctx = context()
    base = result(ctx)
    assert base.listing is not None
    unstamped = base.model_copy(
        update={"listing": base.listing.model_copy(update={"listing_id": None})}
    )
    checked = _check(unstamped, ctx)
    assert isinstance(checked, Trusted) and checked.listing is not None
    assert (checked.listing.listing_id, checked.listing.created_at) == (ctx.listing_id, NOW)


def test_agent_run_id_kept_only_if_persisted() -> None:
    ctx = context()
    base = result(ctx)
    assert base.listing is not None
    kept = _check(base, ctx)
    assert isinstance(kept, Trusted) and kept.listing is not None
    assert kept.listing.generated_by_agent_run_id in {r.id for r in kept.records}
    stray = base.model_copy(
        update={
            "listing": base.listing.model_copy(update={"generated_by_agent_run_id": uuid.uuid4()})
        }
    )
    dropped = _check(stray, ctx)
    assert isinstance(dropped, Trusted) and dropped.listing is not None
    assert dropped.listing.generated_by_agent_run_id is None


def test_salvage_records_valid_and_invalid() -> None:
    ctx = context()
    base = result(ctx)
    salvaged = salvage_records(base, ctx, claim_started_at=CLAIM, now=NOW)
    assert salvaged is not None and len(salvaged) == 4
    many = base.model_copy(update={"agent_runs": list(base.agent_runs) * 30})
    assert salvage_records(many, ctx, claim_started_at=CLAIM, now=NOW) is None
    bad = GenerationResult.model_construct(**{**dict(base), "agent_runs": [{"id": "nope"}]})
    assert salvage_records(bad, ctx, claim_started_at=CLAIM, now=NOW) is None


_mutations = st.fixed_dictionaries(
    {},
    optional={
        "job_id": st.one_of(st.uuids(), st.none(), st.text(max_size=5)),
        "request_id": st.one_of(st.text(max_size=70), st.none(), st.integers()),
        "attempts_used": st.one_of(st.integers(-5, 9), st.text(max_size=3)),
        "terminal_state": st.one_of(st.sampled_from(["generated", "blocked", "x"]), st.none()),
        "agent_runs": st.one_of(st.none(), st.lists(st.integers(), max_size=3)),
        "listing": st.one_of(st.none(), st.integers()),
    },
)


@settings(max_examples=80, deadline=None)
@given(mutation=_mutations)
def test_check_result_never_raises(mutation: dict[str, Any]) -> None:
    ctx = context()
    base = result(ctx)
    mutated = GenerationResult.model_construct(**{**dict(base), **mutation})
    assert isinstance(_check(mutated, ctx), Trusted | Untrusted)
