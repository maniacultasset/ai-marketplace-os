"""Hash-seeded deterministic vectors for the seed and the isolation suite (CANON.md:80)."""

from __future__ import annotations

import math

import pytest
from app.core.config import EMBEDDING_DIMENSIONS
from app.core.vectors import hash_vector
from hypothesis import given, settings
from hypothesis import strategies as st


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def test_deterministic() -> None:
    assert hash_vector("acme:tone_rule:0") == hash_vector("acme:tone_rule:0")


def test_length_1536() -> None:
    assert len(hash_vector("k")) == EMBEDDING_DIMENSIONS == 1536
    assert len(hash_vector("k", dims=3)) == 3


def test_unit_norm() -> None:
    assert math.isclose(_norm(hash_vector("k")), 1.0, rel_tol=1e-9)


def test_distinct_keys_differ() -> None:
    assert hash_vector("a") != hash_vector("b")


def test_unicode_key() -> None:
    vector = hash_vector("भारत:tone")
    assert len(vector) == EMBEDDING_DIMENSIONS and all(math.isfinite(x) for x in vector)


def test_empty_key_valueerror() -> None:
    with pytest.raises(ValueError, match="key"):
        hash_vector("")


@pytest.mark.parametrize("dims", [0, -1])
def test_bad_dims_valueerror(dims: int) -> None:
    with pytest.raises(ValueError, match="dims"):
        hash_vector("k", dims=dims)


@settings(max_examples=50, deadline=None)
@given(st.text(min_size=1, max_size=40))
def test_unit_norm_property(key: str) -> None:
    vector = hash_vector(key)
    assert all(-1.0 <= x <= 1.0 for x in vector)
    assert math.isclose(_norm(vector), 1.0, rel_tol=1e-9)
