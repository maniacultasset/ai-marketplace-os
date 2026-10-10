"""S26 T23 (AC 11, DW6; PLAN D9): scripts/ci/mutation_report.py, the per-module mutation score
over mutmut 3.8.0's results (`mutants/<path>.meta`, `exit_code_by_key`) and the waivers file."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULES = ["app/services/rules_engine/*", "app/services/maker_checker.py"]
HEADER = (
    "# Mutation waivers\n\n"
    "| module | release | score | reason | user | date |\n"
    "|---|---|---|---|---|---|\n"
)


def _report() -> ModuleType:
    if "mutation_report" in sys.modules:
        return sys.modules["mutation_report"]
    spec = importlib.util.spec_from_file_location(
        "mutation_report", REPO_ROOT / "scripts/ci/mutation_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["mutation_report"] = module
    spec.loader.exec_module(module)
    return module


def _meta(root: Path, path: str, codes: dict[str, int | None]) -> None:
    target = root / "mutants" / f"{path}.meta"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "exit_code_by_key": codes,
                "hash_by_function_name": {},
                "type_check_error_by_key": {},
                "durations_by_key": {},
                "estimated_durations_by_key": {},
            }
        ),
        encoding="utf-8",
    )


def _codes(killed: int, survived: int, **other: int) -> dict[str, int | None]:
    codes: dict[str, int | None] = {}
    for n in range(killed):
        codes[f"k{n}"] = 1
    for n in range(survived):
        codes[f"s{n}"] = 0
    for name, (code, count) in {
        "timeout": (36, other.get("timeout", 0)),
        "typed": (37, other.get("typed", 0)),
        "skipped": (34, other.get("skipped", 0)),
        "no_tests": (33, other.get("no_tests", 0)),
    }.items():
        for n in range(count):
            codes[f"{name}{n}"] = code
    return codes


def _run(tmp_path: Path, waivers: str = HEADER) -> tuple[int, list[str]]:
    report = _report()
    waiver_file = tmp_path / "MUTATION-WAIVERS.md"
    waiver_file.write_text(waivers, encoding="utf-8")
    lines: list[str] = []
    code = report.run(tmp_path, MODULES, waiver_file, out=lines.append)
    return code, lines


def test_mutation_report_score_per_module(tmp_path: Path) -> None:
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(8, 1))
    _meta(tmp_path, "app/services/rules_engine/resolver.py", _codes(9, 2, timeout=1, typed=1))
    _meta(tmp_path, "app/services/maker_checker.py", _codes(9, 1, skipped=3))
    _meta(tmp_path, "app/services/other.py", _codes(0, 9))  # not a core module: ignored
    code, lines = _run(tmp_path)
    assert code == 0, lines
    scores = _report().scores(tmp_path, MODULES)
    rules = scores["app/services/rules_engine"]
    # killed + timeout + caught by type check, over every mutant but the skipped ones
    assert (rules.detected, rules.total) == (8 + 9 + 1 + 1, 9 + 13)
    checker = scores["app/services/maker_checker.py"]
    assert (checker.detected, checker.total, checker.skipped) == (9, 10, 3)
    assert any("app/services/rules_engine" in line and "19/22" in line for line in lines)
    assert any("app/services/maker_checker.py" in line and "90.0%" in line for line in lines)


def test_mutation_report_below_80_fails(tmp_path: Path) -> None:
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(9, 1))
    _meta(tmp_path, "app/services/maker_checker.py", _codes(7, 3))  # 70%
    code, lines = _run(tmp_path)
    assert code == 1
    assert any("FAILED" in line and "app/services/maker_checker.py" in line for line in lines)


def test_mutation_report_waiver_row_passes(tmp_path: Path) -> None:
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(9, 1))
    _meta(tmp_path, "app/services/maker_checker.py", _codes(7, 3))
    row = (
        "| app/services/maker_checker.py | 1.0 | 70.0 | equivalent mutants | user | 2026-10-10 |\n"
    )
    code, lines = _run(tmp_path, HEADER + row)
    assert code == 0, lines
    assert any("waived" in line and "app/services/maker_checker.py" in line for line in lines)


def test_mutation_report_waiver_below_its_score_fails(tmp_path: Path) -> None:
    """A waiver holds the score it was granted at: a lower score fails again."""
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(9, 1))
    _meta(tmp_path, "app/services/maker_checker.py", _codes(6, 4))
    row = (
        "| app/services/maker_checker.py | 1.0 | 70.0 | equivalent mutants | user | 2026-10-10 |\n"
    )
    code, _ = _run(tmp_path, HEADER + row)
    assert code == 1


@pytest.mark.parametrize(
    "row",
    [
        "| app/services/maker_checker.py | 1.0 | 70.0 | reason | user |\n",  # a cell missing
        "| app/services/maker_checker.py | 1.0 | high | reason | user | 2026-10-10 |\n",
        "| app/services/maker_checker.py | 1.0 | 70.0 |  | user | 2026-10-10 |\n",
        "| app/services/maker_checker.py | 1.0 | 70.0 | reason |  | 2026-10-10 |\n",
        "| app/services/maker_checker.py | 1.0 | 70.0 | reason | user | 10/10/2026 |\n",
        "| app/services/unknown.py | 1.0 | 70.0 | reason | user | 2026-10-10 |\n",
    ],
)
def test_mutation_report_malformed_waiver_fails(tmp_path: Path, row: str) -> None:
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(9, 1))
    _meta(tmp_path, "app/services/maker_checker.py", _codes(9, 1))
    code, lines = _run(tmp_path, HEADER + row)
    assert code == 1
    assert any("waiver" in line.lower() for line in lines)


def test_mutation_report_zero_mutants_is_failure(tmp_path: Path) -> None:
    _meta(tmp_path, "app/services/rules_engine/text.py", _codes(9, 1))
    code, lines = _run(tmp_path)  # maker_checker has no results at all
    assert code == 1
    assert any("no mutants" in line and "app/services/maker_checker.py" in line for line in lines)


def test_mutation_report_unchecked_mutants_fail(tmp_path: Path) -> None:
    """A run that did not check every mutant is not a score."""
    codes = _codes(9, 1)
    codes["pending"] = None
    _meta(tmp_path, "app/services/rules_engine/text.py", codes)
    _meta(tmp_path, "app/services/maker_checker.py", _codes(9, 1))
    code, lines = _run(tmp_path)
    assert code == 1
    assert any("not checked" in line for line in lines)


def test_mutation_report_committed_waivers_file_parses() -> None:
    report = _report()
    waivers = report.load_waivers(REPO_ROOT / "docs/review/MUTATION-WAIVERS.md", known=None)
    assert isinstance(waivers, dict)


def test_rules_seed_path_found_from_a_deeper_copy(tmp_path: Path) -> None:
    """The CLI's rules file resolves from apps/api and from a copy one level deeper (mutants/)."""
    from app.cli import RULES_SEED_PATH, _rules_seed_path

    assert RULES_SEED_PATH == REPO_ROOT / "packages/schemas/fixtures/rules.seed.json"
    deeper = REPO_ROOT / "apps/api/mutants/app/cli.py"
    assert _rules_seed_path(deeper) == RULES_SEED_PATH
    nowhere = tmp_path / "a/b/c/d/cli.py"
    assert (
        _rules_seed_path(nowhere)
        == nowhere.parents[3] / "packages/schemas/fixtures/rules.seed.json"
    )
