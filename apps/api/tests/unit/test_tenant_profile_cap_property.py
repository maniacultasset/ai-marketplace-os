"""S16 tester round 2 (fix F2): whatever sequence of voice PATCHes the service accepts, the stored
profile is one the `brand_embed` profile body can chunk (`check_chunks` at `MAX_PROFILE_CHUNKS`),
so an accepted PATCH never leaves a job that fails with `VALIDATION_FAILED`."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from app.core.errors import InputError
from app.services import tenants
from app.services.brand_chunking import MAX_PROFILE_CHUNKS, check_chunks, chunk_profile
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


def _glossary(n: int, off: int) -> dict[str, str]:
    return {f"term{off}-{i}": f"pref{off}-{i}" for i in range(n)}


def _guidelines(n: int, off: int, dup: bool) -> list[str]:
    return ["Guideline repeated." if dup else f"Guideline {off}-{i}." for i in range(n)]


def _tone(n: int, off: int) -> str:
    return "\n".join(f"- t{off}-{i}" for i in range(n))


_patch = st.one_of(
    st.builds(lambda n, o: {"glossary": _glossary(n, o)}, st.integers(0, 500), st.integers(0, 3)),
    st.builds(
        lambda n, o, d: {"guidelines": _guidelines(n, o, d)},
        st.integers(0, 100),
        st.integers(0, 3),
        st.booleans(),
    ),
    st.builds(
        lambda n, o: {"tone": _tone(n, o) if n else None}, st.integers(0, 250), st.integers(0, 3)
    ),
    st.just({"name": "Acme"}),
)


def _apply(view: tenants.TenantView, fields: dict[str, Any]) -> tenants.TenantView | None:
    try:
        tenants._check_voice(fields)
        tenants._check_merged(view, fields)
    except InputError:
        return None
    return dataclasses.replace(view, **fields)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_patch, min_size=1, max_size=6))
def test_accepted_patch_sequence_always_chunkable(patches: list[dict[str, Any]]) -> None:
    view = tenants.TenantView(uuid.uuid4(), "Acme", "acme", None, [], {}, "active")
    for fields in patches:
        nxt = _apply(view, fields)
        if nxt is not None:
            view = nxt
        drafts = chunk_profile(view.tone, view.guidelines, view.glossary)
        if drafts:
            check_chunks(drafts, field="profile", max_chunks=MAX_PROFILE_CHUNKS)


def test_refused_patch_is_exactly_the_over_cap_one() -> None:
    view = tenants.TenantView(
        uuid.uuid4(), "Acme", "acme", None, _guidelines(100, 0, False), _glossary(500, 0), "active"
    )
    assert _apply(view, {"tone": _tone(100, 0)}) is not None  # 700: at the cap
    assert _apply(view, {"tone": _tone(101, 0)}) is None  # 701: over
    # duplicates collapse in the chunker, so 100 copies of one guideline count once
    dup = dataclasses.replace(view, guidelines=_guidelines(100, 0, True))
    assert len(chunk_profile(None, dup.guidelines, None)) == 1
    assert _apply(dup, {"tone": _tone(199, 0)}) is not None  # 1 + 500 + 199 = 700
    assert _apply(dup, {"tone": _tone(200, 0)}) is None
