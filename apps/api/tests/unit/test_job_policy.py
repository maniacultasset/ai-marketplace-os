"""S13 A1 (AC4; PLAN Decisions 5-6): the per-kind job policy, backoff and the body registry."""

from __future__ import annotations

import pytest
from app.workers import policy, registry
from app.workers.policy import LEASE_GRACE_S, POLICIES, JobPolicy, backoff_s, lease_s
from hypothesis import given
from hypothesis import strategies as st
from mkos_schemas.enums import JobKind

# PLAN Decision 5, written out: kind -> (queue, timeout_s, max_attempts, retry_transport).
PD5: dict[str, tuple[str, int, int, bool]] = {
    "listing_generate": ("default", 300, 1, False),
    "listing_publish": ("publish", 600, 3, True),
    "listing_status_sync": ("publish", 120, 3, True),
    "bulk_import": ("default", 1800, 3, True),
    "image_compliance": ("default", 300, 3, True),
    "brand_embed": ("default", 300, 3, True),
}


def test_policies_literal_table() -> None:
    got = {
        kind.value: (p.queue, p.timeout_s, p.max_attempts, p.retry_transport)
        for kind, p in POLICIES.items()
    }
    assert got == PD5
    assert all(p.kind is kind for kind, p in POLICIES.items())


def test_generation_max_attempts_1() -> None:
    assert POLICIES[JobKind.LISTING_GENERATE].max_attempts == 1


def test_policy_queue_matches_trigger() -> None:
    # job_runs_set_queue() (revision 0003): the publish family runs on `publish`, the rest on `default`.
    for kind, p in POLICIES.items():
        expected = (
            "publish"
            if kind in {JobKind.LISTING_PUBLISH, JobKind.LISTING_STATUS_SYNC}
            else "default"
        )
        assert p.queue == expected, kind


def test_lease_is_timeout_plus_60() -> None:
    assert LEASE_GRACE_S == 60
    for p in POLICIES.values():
        assert lease_s(p) == p.timeout_s + 60


def test_backoff_doubles_and_caps_60() -> None:
    # rand = 1.0 is the top of the jitter band: the undisturbed 1 s doubling, capped at 60 s.
    assert [backoff_s(a, 1.0) for a in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert backoff_s(3, 0.0) == 2.0


@given(st.integers(min_value=1, max_value=50), st.floats(min_value=0.0, max_value=1.0))
def test_backoff_within_jitter_bounds(attempt: int, rand: float) -> None:
    base = min(60, 2 ** (attempt - 1))
    assert base * 0.5 <= backoff_s(attempt, rand) <= base


def test_backoff_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="attempt"):
        backoff_s(0, 0.5)
    with pytest.raises(ValueError, match="rand"):
        backoff_s(1, 1.5)


def test_job_policy_is_frozen() -> None:
    p = POLICIES[JobKind.BULK_IMPORT]
    assert isinstance(p, JobPolicy)
    with pytest.raises(AttributeError):
        p.timeout_s = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.POLICIES[JobKind.BULK_IMPORT] = p  # type: ignore[index]


def test_register_rejects_unknown_kind() -> None:
    async def body(ctx: registry.JobContext) -> registry.Outcome:
        return registry.Outcome("succeeded")

    with pytest.raises(ValueError, match="unknown job kind"):
        registry.register("not_a_kind", body)  # type: ignore[arg-type]


def test_register_and_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "REGISTRY", {})

    async def body(ctx: registry.JobContext) -> registry.Outcome:
        return registry.Outcome("partial", {"n": 1})

    registry.register(JobKind.BRAND_EMBED, body)
    assert registry.REGISTRY[JobKind.BRAND_EMBED] is body
    assert registry.body_for(JobKind.BRAND_EMBED) is body
    assert registry.body_for(JobKind.BULK_IMPORT) is None


def test_outcome_status_is_success_only() -> None:
    with pytest.raises(ValueError, match="succeeded or partial"):
        registry.Outcome("failed")  # type: ignore[arg-type]
