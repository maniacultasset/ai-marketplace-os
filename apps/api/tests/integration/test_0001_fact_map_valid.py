"""`fact_map_valid(m, require_value)`, the SQL twin of the fact envelope (DATABASE.md §5.6 "Fact
envelope", "SQL twin"). Accept rows prove it is not over-strict, reject rows that it is not lax,
and the property test that no JSON document makes it raise."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg.types.json import Jsonb

from .factories import fact

pytestmark = pytest.mark.allow_hosts(["localhost", "127.0.0.1", "::1"])

Conn = psycopg.Connection[tuple[Any, ...]]

SNAPSHOT = {"import_id": "i1", "row_number": 4, "header": "Fabric", "cell": "cotton"}

ACCEPT: list[tuple[str, Any, bool]] = [
    ("empty map", {}, True),
    ("seller fact", {"fabric": fact()}, True),
    ("no value when not required", {"mrp": fact(value=None)}, False),
    ("normalized only", {"fabric": fact(value=None, normalized="cotton")}, True),
    ("document with image ref", {"fabric": fact("document", evidence_ref="image:abc")}, True),
    (
        "import_row with snapshot",
        {"fabric": fact("pim", evidence_ref="import_row:1", evidence_snapshot=SNAPSHOT)},
        True,
    ),
    ("agent with run id", {"fabric": fact(captured_via="agent", agent_run_id="r1")}, True),
    (
        "unit and vocabulary normalization",
        {"net_quantity": fact(unit="g", normalized="250", normalized_by="vocabulary")},
        True,
    ),
    (
        "ai normalization with confidence",
        {"color": fact(normalized="black", normalized_by="ai", confidence=0.7)},
        True,
    ),
    (
        "confirmed pair",
        {"fabric": fact(confirmed_by="u1", confirmed_at="2026-10-05T00:00:00Z")},
        True,
    ),
    (
        "ai with confidence 0 and 1",
        {"a1": fact("ai", confidence=0), "a2": fact("ai", confidence=1)},
        True,
    ),
    ("64-char key", {"k" + "a" * 63: fact()}, True),
    (
        "2048-byte snapshot",
        {"fabric": fact("pim", evidence_ref="import_row:1", evidence_snapshot={"c": "x" * 2039})},
        True,
    ),
    (
        "every evidence kind",
        {
            f"k_{kind}": fact("pim", evidence_ref=f"{kind}:x-1.2_3:4")
            | (
                {"evidence_snapshot": {}}
                if kind in ("import_row", "human_task", "agent_run")
                else {}
            )
            for kind in (
                "image",
                "import_row",
                "human_task",
                "listing",
                "agent_run",
                "rule",
                "brand_profile",
            )
        },
        True,
    ),
]

REJECT: list[tuple[str, Any, bool]] = [
    ("array", [], False),
    ("string", "x", False),
    ("json null", None, False),
    ("key A", {"A": fact()}, False),
    ("key 1a", {"1a": fact()}, False),
    ("65-char key", {"k" + "a" * 64: fact()}, False),
    ("fact not an object", {"fabric": "cotton"}, False),
    ("missing source", {"fabric": fact(source=None)}, False),
    ("missing captured_via", {"fabric": fact(captured_via=None)}, False),
    ("missing updated_at", {"fabric": fact(updated_at=None)}, False),
    ("updated_at not a string", {"fabric": fact(updated_at=1)}, False),
    ("bad source", {"fabric": fact(source="model")}, False),
    ("bad captured_via", {"fabric": fact(captured_via="email")}, False),
    ("source not a string", {"fabric": fact(source=["seller"])}, False),
    ("confidence -0.01", {"fabric": fact("ai", confidence=-0.01)}, False),
    ("confidence 1.01", {"fabric": fact("ai", confidence=1.01)}, False),
    ("confidence string", {"fabric": fact("ai", confidence="0.5")}, False),
    ("ai without confidence", {"fabric": fact("ai", confidence=None)}, False),
    ("derived without confidence", {"fabric": fact("derived", confidence=None)}, False),
    ("document without ref", {"fabric": fact("document", evidence_ref=None)}, False),
    ("url ref", {"fabric": fact("document", evidence_ref="url:x")}, False),
    ("empty ref id", {"fabric": fact("document", evidence_ref="image:")}, False),
    ("fragment ref", {"fabric": fact("document", evidence_ref="image:a#b")}, False),
    ("ref not a string", {"fabric": fact("document", evidence_ref=1)}, False),
    ("import_row without snapshot", {"fabric": fact("pim", evidence_ref="import_row:1")}, False),
    (
        "2049-byte snapshot",
        {"fabric": fact("pim", evidence_ref="import_row:1", evidence_snapshot={"c": "x" * 2040})},
        False,
    ),
    (
        "snapshot not an object",
        {"fabric": fact("pim", evidence_ref="import_row:1", evidence_snapshot="row 1")},
        False,
    ),
    ("agent without run id", {"fabric": fact(captured_via="agent", agent_run_id=None)}, False),
    ("run id not a string", {"fabric": fact(captured_via="agent", agent_run_id=7)}, False),
    ("confirmed_by alone", {"fabric": fact(confirmed_by="u1")}, False),
    ("confirmed_at alone", {"fabric": fact(confirmed_at="2026-10-05T00:00:00Z")}, False),
    ("normalized_by model", {"fabric": fact(normalized_by="model")}, False),
    ("normalized_by AI", {"fabric": fact(normalized_by="AI")}, False),
    ("ai normalization without confidence", {"fabric": fact(normalized_by="ai")}, False),
    ("unit not a string", {"net_quantity": fact(unit=5)}, False),
    ("unknown key", {"fabric": fact(note="hi")}, False),
    ("larger than 128 KiB", {"fabric": fact(value="x" * 131072)}, False),
    ("no value or normalized", {"fabric": fact(value=None)}, True),
]


def _valid(conn: Conn, document: Any, require_value: bool) -> Any:
    row = conn.execute("SELECT fact_map_valid(%s, %s)", (Jsonb(document), require_value)).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    ("document", "require_value", "expected"),
    [pytest.param(d, r, True, id=f"accept: {n}") for n, d, r in ACCEPT]
    + [pytest.param(d, r, False, id=f"reject: {n}") for n, d, r in REJECT],
)
def test_fact_map_valid_cases(db: Conn, document: Any, require_value: bool, expected: bool) -> None:
    assert _valid(db, document, require_value) is expected


def test_fact_map_valid_sql_null(db: Conn) -> None:
    assert db.execute("SELECT fact_map_valid(NULL, true)").fetchone() == (False,)
    assert db.execute("SELECT fact_map_valid('{}', NULL)").fetchone() == (False,)


def test_check_functions_begin_atomic(db: Conn) -> None:
    rows = db.execute(
        "SELECT p.proname, p.prosqlbody IS NOT NULL, p.provolatile, p.proparallel "
        "FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
        "AND p.proname IN ('fact_map_valid', 'gtin_valid') ORDER BY 1"
    ).fetchall()
    assert rows == [("fact_map_valid", True, "i", "s"), ("gtin_valid", True, "i", "s")]


FIELDS = [
    "value",
    "normalized",
    "unit",
    "source",
    "confidence",
    "normalized_by",
    "evidence_ref",
    "evidence_snapshot",
    "captured_via",
    "agent_run_id",
    "confirmed_by",
    "confirmed_at",
    "updated_at",
    "other",
]
_text = st.text(max_size=12).filter(lambda s: "\x00" not in s)
_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
    _text,
    st.sampled_from(
        ["seller", "ai", "derived", "document", "agent", "form", "image:a", "import_row:1"]
    ),
)
_json = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3), st.dictionaries(_text, children, max_size=3)
    ),
    max_leaves=12,
)
_fact_like = st.dictionaries(st.sampled_from(FIELDS), _json, max_size=8)
_documents = st.one_of(
    _json,
    st.dictionaries(
        st.one_of(_text, st.sampled_from(["mrp", "fabric", "A"])),
        st.one_of(_fact_like, _json),
        max_size=4,
    ),
)


@settings(
    max_examples=400, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(document=_documents, require_value=st.booleans())
def test_fact_map_valid_never_raises(db: Conn, document: Any, require_value: bool) -> None:
    assert _valid(db, document, require_value) in (True, False)
