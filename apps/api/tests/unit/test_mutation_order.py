"""tests/support/mutation_order.py: a mutmut child's test order (S26 T23 gate-fix)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tests.support.mutation_order import mutation_order


@dataclass(frozen=True)
class _Item:
    nodeid: str


def _ids(items: list[_Item]) -> list[str]:
    return [i.nodeid for i in items]


def test_unit_files_first_cheapest_first_file_order_kept(tmp_path: Path) -> None:
    stats = tmp_path / "mutmut-stats.json"
    stats.write_text(
        json.dumps(
            {
                "duration_by_test": {
                    "tests/unit/test_slow.py::a": 5.0,
                    "tests/unit/test_fast.py::b": 0.1,
                    "tests/unit/test_fast.py::a": 0.2,
                    "tests/integration/test_db.py::a": 0.01,
                }
            }
        ),
        encoding="utf-8",
    )
    items = [
        _Item("tests/integration/test_db.py::a"),
        _Item("tests/unit/test_slow.py::a"),
        _Item("tests/unit/test_fast.py::b"),
        _Item("tests/unit/test_fast.py::a"),
    ]
    assert _ids(mutation_order(items, stats)) == [
        "tests/unit/test_fast.py::b",
        "tests/unit/test_fast.py::a",
        "tests/unit/test_slow.py::a",
        "tests/integration/test_db.py::a",
    ]


def test_without_stats_only_unit_before_integration_by_path(tmp_path: Path) -> None:
    items = [
        _Item("tests/integration/test_b.py::x"),
        _Item("tests/unit/test_z.py::y"),
        _Item("tests/integration/test_a.py::x"),
        _Item("tests/unit/test_z.py::x"),
    ]
    for stats in (tmp_path / "missing.json", tmp_path / "bad.json"):
        if stats.name == "bad.json":
            stats.write_text("{not json", encoding="utf-8")
        ordered = mutation_order(items, stats)
        assert _ids(ordered) == [
            "tests/unit/test_z.py::y",
            "tests/unit/test_z.py::x",
            "tests/integration/test_a.py::x",
            "tests/integration/test_b.py::x",
        ]
        assert sorted(_ids(ordered)) == sorted(_ids(items))
