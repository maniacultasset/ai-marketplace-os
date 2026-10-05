"""The extension version gate 0002b runs before building the HNSW index (DATABASE.md §8.2 ledger
row 0002b: "asserts pgvector >= 0.8.2"): versions compare as integer tuples, never as text."""

from __future__ import annotations

from typing import Any, cast

import pytest
from app.db.migrations import (
    ExtensionTooOld,
    parse_extension_version,
    require_extension_version,
)
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Connection

MINIMUM = (0, 8, 2)


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Connection:
    """Answers the pg_extension lookup with `version` (None: not installed)."""

    def __init__(self, version: str | None) -> None:
        self.version = version
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, statement: object, params: dict[str, Any] | None = None) -> _Result:
        self.executed.append((str(statement), params))
        return _Result(self.version)


def _gate(version: str | None, minimum: tuple[int, ...] = MINIMUM) -> _Connection:
    fake = _Connection(version)
    require_extension_version(cast(Connection, fake), "vector", minimum)
    return fake


@pytest.mark.parametrize(
    ("version", "ok"),
    [("0.8.1", False), ("0.8.2", True), ("0.8.5", True), ("0.10.0", True), ("1.0.0", True)],
    ids=["0.8.1 refuse", "0.8.2 ok", "0.8.5 ok", "0.10.0 ok", "1.0.0 ok"],
)
def test_version_gate(version: str, ok: bool) -> None:
    if ok:
        fake = _gate(version)
        sql, params = fake.executed[0]
        assert "pg_extension" in sql and params == {"name": "vector"}
        return
    with pytest.raises(ExtensionTooOld, match=r"^pgvector 0\.8\.1 found; 0002b needs >= 0\.8\.2$"):
        _gate(version)


@pytest.mark.parametrize("version", ["0.7.4", "0.8", "0.8.1.9"])
def test_version_gate_refuses_older(version: str) -> None:
    with pytest.raises(ExtensionTooOld, match=f"^pgvector {version} found"):
        _gate(version)


def test_version_gate_missing_extension_refuses() -> None:
    with pytest.raises(ExtensionTooOld, match=r"^pgvector not installed; 0002b needs >= 0\.8\.2$"):
        _gate(None)


@pytest.mark.parametrize("version", ["", "0.8.x", "v0.8.2", "0..8", "0.8.2-rc1", " 0.8.2"])
def test_version_gate_unparseable_refuses(version: str) -> None:
    with pytest.raises(ValueError, match="not a numeric version"):
        parse_extension_version(version)
    with pytest.raises(ExtensionTooOld, match="found; 0002b needs"):
        _gate(version)


def test_parse_extension_version() -> None:
    assert parse_extension_version("0.8.5") == (0, 8, 5)
    assert parse_extension_version("0.10.0") == (0, 10, 0)
    assert parse_extension_version("12") == (12,)


_VERSIONS = st.lists(st.integers(min_value=0, max_value=999), min_size=1, max_size=4)


@given(_VERSIONS, _VERSIONS)
def test_version_order_matches_tuple_order(left: list[int], right: list[int]) -> None:
    """The gate accepts exactly when the found version's integer tuple is >= the minimum's."""
    found = ".".join(str(part) for part in left)
    accepted = True
    try:
        _gate(found, tuple(right))
    except ExtensionTooOld:
        accepted = False
    assert parse_extension_version(found) == tuple(left)
    assert accepted == (tuple(left) >= tuple(right))
